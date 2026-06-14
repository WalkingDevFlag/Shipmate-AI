"""Inner verify-loop (Phase 4).

The loop iterates the Coder against cheap checks (lint + scope + sandbox smoke),
feeding concrete errors back, bounded by max_iters AND a token budget, BEFORE
the expensive full gate / PR. It unifies what were three scattered one-shot
retries.

These tests drive the loop with PURE stubs (no Bedrock, no subprocess): the
checks and the feedback callback are injected, exactly as the orchestrator
injects the real lint/scope/smoke + run_with_lint_feedback. They pin:
  • a clean first output → 0 iterations, passed;
  • a broken-then-fixed patch → converges within the cap;
  • the iteration cap and the token budget both stop the loop with the right
    stop_reason;
  • a fatal check (syntax error) stops immediately;
  • a coder that returns nothing usable stops as coder_failed;
  • a check that raises is fail-open (treated as pass).
"""
import pytest

from app.services import verify_loop
from app.services.verify_loop import (
    CheckResult, VerifyLoopResult, run_verify_loop,
)


# ── Minimal coder_out stub (duck-typed: .files / .summary) ───────────────────

class _File:
    def __init__(self, path="x.py", new_content="X = 1\n", rationale="r"):
        self.path = path
        self.new_content = new_content
        self.rationale = rationale


class _Out:
    def __init__(self, files=None, summary="a summary"):
        self.files = files if files is not None else [_File()]
        self.summary = summary


# ── Check stubs ──────────────────────────────────────────────────────────────

def _clean_check(_out):
    return CheckResult(name="ok")


def _issue_check(issue):
    return lambda _out: CheckResult(name="bad", issues=[issue])


class TestHappyPath:
    def test_clean_first_output_zero_iterations(self):
        res = run_verify_loop(_Out(), [_clean_check], lambda issues: None)
        assert res.passed is True
        assert res.iterations == 0
        assert res.stop_reason == "clean"
        assert res.issues == []

    def test_no_checks_is_clean(self):
        res = run_verify_loop(_Out(), [], lambda issues: None)
        assert res.passed is True and res.iterations == 0


class TestConvergence:
    def test_broken_then_fixed_converges(self):
        # Check fails until the coder "fixes" it (we flip a flag on re-prompt).
        state = {"fixed": False}

        def check(_out):
            return CheckResult(name="c", issues=[] if state["fixed"] else ["bad import"])

        def feedback(issues):
            state["fixed"] = True
            return _Out(summary="fixed")

        res = run_verify_loop(_Out(), [check], feedback, max_iters=2)
        assert res.passed is True
        assert res.iterations == 1
        assert res.stop_reason == "clean"

    def test_feedback_receives_the_issues(self):
        seen = {}

        def check(_out):
            return CheckResult(name="c", issues=["fix the X import", "and Y"]) \
                if not seen.get("done") else CheckResult(name="c")

        def feedback(issues):
            seen["issues"] = issues
            seen["done"] = True
            return _Out()

        run_verify_loop(_Out(), [check], feedback, max_iters=2)
        assert seen["issues"] == ["fix the X import", "and Y"]


class TestBounds:
    def test_iteration_cap_stops_loop(self):
        # Never fixes → must stop at max_iters with stop_reason max_iters.
        calls = {"n": 0}

        def feedback(issues):
            calls["n"] += 1
            return _Out(summary=f"attempt {calls['n']}")

        res = run_verify_loop(
            _Out(), [_issue_check("perma-fail")], feedback, max_iters=2,
        )
        assert res.passed is False
        assert res.iterations == 2
        assert res.stop_reason == "max_iters"
        assert calls["n"] == 2  # exactly max_iters re-prompts, no more

    def test_token_budget_stops_before_reprompt(self, monkeypatch):
        # Force headroom below the floor → loop must stop with stop_reason budget
        # WITHOUT calling the coder.
        monkeypatch.setattr(verify_loop, "_token_headroom", lambda budget: 10)
        called = {"n": 0}

        def feedback(issues):
            called["n"] += 1
            return _Out()

        res = run_verify_loop(
            _Out(), [_issue_check("fail")], feedback,
            max_iters=3, token_budget=100_000, min_token_headroom=8000,
        )
        assert res.passed is False
        assert res.stop_reason == "budget"
        assert called["n"] == 0  # never re-prompted — budget gate fired first

    def test_no_token_budget_means_iteration_cap_only(self, monkeypatch):
        # token_budget=None → _token_headroom returns None → no token gating.
        res = run_verify_loop(
            _Out(), [_issue_check("fail")], lambda i: _Out(),
            max_iters=1, token_budget=None,
        )
        assert res.stop_reason == "max_iters"


class TestFatalAndFailure:
    def test_fatal_check_stops_immediately(self):
        calls = {"n": 0}

        def feedback(issues):
            calls["n"] += 1
            return _Out()

        res = run_verify_loop(
            _Out(),
            [lambda _o: CheckResult(name="lint", issues=["syntax error"], fatal=True)],
            feedback, max_iters=3,
        )
        assert res.passed is False
        assert res.stop_reason == "fatal"
        assert calls["n"] == 0  # fatal → no re-prompt at all

    def test_coder_returns_nothing_stops_as_coder_failed(self):
        res = run_verify_loop(
            _Out(), [_issue_check("fail")], lambda issues: None, max_iters=3,
        )
        assert res.passed is False
        assert res.stop_reason == "coder_failed"
        assert res.iterations == 1

    def test_coder_returns_empty_files_stops(self):
        res = run_verify_loop(
            _Out(), [_issue_check("fail")], lambda issues: _Out(files=[]),
            max_iters=3,
        )
        assert res.stop_reason == "coder_failed"

    def test_coder_callback_raising_is_caught(self):
        def feedback(issues):
            raise RuntimeError("provider down")
        res = run_verify_loop(_Out(), [_issue_check("fail")], feedback)
        assert res.passed is False
        assert res.stop_reason == "coder_failed"


class TestFailOpen:
    def test_check_that_raises_is_treated_as_pass(self):
        def boom(_out):
            raise ValueError("checker bug")
        res = run_verify_loop(_Out(), [boom, _clean_check], lambda i: None)
        # The raising check is ignored; the clean check passes → loop is clean.
        assert res.passed is True
        assert res.stop_reason == "clean"

    def test_best_output_always_returned(self):
        # Even on failure, coder_out is the latest real patch (never None).
        last = _Out(summary="latest")
        res = run_verify_loop(
            _Out(summary="first"), [_issue_check("fail")],
            lambda i: last, max_iters=1,
        )
        assert res.coder_out is last


class TestFailedChecks:
    """The loop tags WHICH checks failed so the caller classifies a rejection
    by check identity (lint vs scope), not by sniffing issue text — the fix for
    scope_guard's catch-all 'DELETES N of M lines' message being misread."""

    def test_failed_checks_names_the_failing_check(self):
        def scope_check(_out):
            return CheckResult(name="scope", issues=["README DELETES 600 of 620 lines"])
        res = run_verify_loop(_Out(), [scope_check], lambda i: None, max_iters=0)
        assert res.passed is False
        assert "scope" in res.failed_checks

    def test_multiple_failed_checks_all_named(self):
        checks = [
            lambda o: CheckResult(name="lint", issues=["bad import"]),
            lambda o: CheckResult(name="scope", issues=["dropped def"]),
        ]
        res = run_verify_loop(_Out(), checks, lambda i: None, max_iters=0)
        assert set(res.failed_checks) == {"lint", "scope"}

    def test_clean_has_no_failed_checks(self):
        res = run_verify_loop(_Out(), [_clean_check], lambda i: None)
        assert res.failed_checks == []


class TestHistory:
    def test_history_records_each_iteration(self):
        state = {"n": 0}

        def check(_out):
            state["n"] += 1
            return CheckResult(name="c", issues=[] if state["n"] >= 2 else ["x"])

        res = run_verify_loop(_Out(), [check], lambda i: _Out(), max_iters=3)
        # iter0 had an issue, iter1 clean → 2 history entries.
        assert len(res.history) == 2
        assert res.history[0] == ["x"]
        assert res.history[1] == []
