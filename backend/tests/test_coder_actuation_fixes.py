"""Coder actuation reliability fixes — the four issues a dogfooding loop surfaced
where the *harness* (not the model) blocked or mis-reported good work:

  A. Lint counted only the existing GitHub tree, so a patch that CREATES a module
     and imports it in the same patch (the textbook "extract to shared util"
     refactor) was wrongly rejected as a hallucinated import.
  B. The Coder LLM calls had no per-call deadline — a hung provider pinned the
     request/SSE for the full provider read-timeout (observed ~900s).
  C1. Large target files were always reprinted in full (no diff), which is what
     blew the timeout and tempted the model to bail; diff mode now auto-engages.
  C2. An empty patch carrying a clean `VERIFY:` stamp (a "phantom patch") was
     reported as a benign no_change; it's now detected, retried once, and its
     misleading clean stamp is stripped from the user-facing summary.
"""
import asyncio

import pytest

from app.agents.coder_agent import CoderOutput, CoderFile
from app.services import coder_orchestrator as co_mod
from app.services.coder_orchestrator import (
    _lint_coder_output,
    _looks_like_phantom,
    _honest_empty_summary,
    _run_coder,
    _DIFF_AUTO_LINES,
    _LLM_TIMEOUT_S,
)
from app.services.coder_lessons import distill


# ── Patch A — same-patch new-module imports are valid ─────────────────────────
class TestPatchA_SamePatchImports:
    def _extract_refactor(self):
        return CoderOutput(
            files=[
                CoderFile(
                    path="backend/app/services/sse.py",
                    new_content="def sse_frame(o):\n    return f'data: {o}\\n\\n'\n",
                    rationale="new shared util",
                ),
                CoderFile(
                    path="backend/app/api/routes/auto_fix.py",
                    new_content="from app.services.sse import sse_frame\n\nx = sse_frame({})\n",
                    rationale="use the shared util",
                ),
            ],
            summary="extract the duplicated sse frame helper into one module",
        )

    def test_import_of_same_patch_new_module_is_clean(self):
        co = self._extract_refactor()
        # Existing tree does NOT contain sse.py — it's born in this patch.
        tree = ["backend/app/api/routes/auto_fix.py"]
        issues = _lint_coder_output(
            co, {"backend/app/api/routes/auto_fix.py": "old\n"}, tree,
        )
        assert issues == [], f"same-patch import wrongly flagged: {issues}"

    def test_genuinely_absent_module_still_flagged(self):
        # Importing a module that is NEITHER in the tree NOR created in the patch
        # must still be caught — the fix only trusts files actually produced.
        co = CoderOutput(
            files=[CoderFile(
                path="backend/app/api/routes/auto_fix.py",
                new_content="from app.services.does_not_exist import nope\n",
                rationale="bad",
            )],
            summary="imports a hallucinated module that is not created anywhere",
        )
        issues = _lint_coder_output(
            co, {"backend/app/api/routes/auto_fix.py": "old\n"},
            ["backend/app/api/routes/auto_fix.py"],
        )
        assert any("hallucinated" in i for i in issues)


# ── Patch B — per-call LLM deadline ───────────────────────────────────────────
class TestPatchB_Timeout:
    def test_run_coder_raises_timeout_when_call_hangs(self, monkeypatch):
        monkeypatch.setattr(co_mod, "_LLM_TIMEOUT_S", 0.05)

        class _HangingAgent:
            def run(self, *a, **k):
                import time
                time.sleep(5)  # exceeds the 0.05s deadline
                return CoderOutput(files=[], summary="never reached")

        with pytest.raises(asyncio.TimeoutError):
            asyncio.run(_run_coder(_HangingAgent(), object(), "smart", "full"))

    def test_run_coder_returns_normally_under_deadline(self):
        class _FastAgent:
            def run(self, *a, **k):
                return CoderOutput(
                    files=[CoderFile(path="x.py", new_content="x=1\n", rationale="r")],
                    summary="a fast and complete patch returned well under the deadline",
                )

        out = asyncio.run(_run_coder(_FastAgent(), object(), "smart", "full"))
        assert [f.path for f in out.files] == ["x.py"]

    def test_default_deadline_is_below_provider_read_timeout(self):
        # The guard MUST fire before boto's 300s read_timeout so the request is
        # freed first; otherwise it's a no-op.
        assert _LLM_TIMEOUT_S < 300


# ── Patch C1 — auto-engage diff mode on large files ───────────────────────────
class TestPatchC1_DiffSizeGate:
    def test_threshold_constant_sane(self):
        assert 100 <= _DIFF_AUTO_LINES <= 2000

    def test_large_file_trips_the_gate(self):
        # Mirror the orchestrator's predicate exactly.
        target_files = {"big.py": "\n".join(f"x{i}=1" for i in range(_DIFF_AUTO_LINES + 50))}
        big = any((c or "").count("\n") + 1 > _DIFF_AUTO_LINES for c in target_files.values())
        assert big is True

    def test_small_files_do_not_trip_the_gate(self):
        target_files = {"a.py": "x=1\n", "b.py": "y=2\n"}
        big = any((c or "").count("\n") + 1 > _DIFF_AUTO_LINES for c in target_files.values())
        assert big is False


# ── Patch C2 — phantom-patch detection / honest summary ───────────────────────
class TestPatchC2_Phantom:
    def _phantom(self):
        return CoderOutput(
            files=[], skipped=["a.py", "b.py", "c.py"],
            summary=("Wraps the three asyncio.to_thread calls with asyncio.wait_for. "
                     "VERIFY: imports-grounded, no-theater, scope-ok, deps-ok"),
        )

    def test_empty_plus_clean_verify_is_phantom(self):
        assert _looks_like_phantom(self._phantom()) is True

    def test_honest_decline_is_not_phantom(self):
        co = CoderOutput(files=[], skipped=["a.py"],
                         summary="DECLINED: spans 4 large files, needs decomposition")
        assert _looks_like_phantom(co) is False

    def test_real_patch_never_phantom(self):
        co = CoderOutput(
            files=[CoderFile(path="x.py", new_content="x=1\n", rationale="r")],
            summary="did it. VERIFY: imports-grounded, no-theater, scope-ok, deps-ok",
        )
        assert _looks_like_phantom(co) is False

    def test_terse_empty_is_not_phantom(self):
        co = CoderOutput(files=[], skipped=["a.py"], summary="no change")
        assert _looks_like_phantom(co) is False

    def test_honest_summary_strips_clean_verify_stamp(self):
        cleaned = _honest_empty_summary(self._phantom())
        assert "verify:" not in cleaned.lower()
        assert "no patch produced" in cleaned.lower()

    def test_honest_summary_preserves_decline(self):
        co = CoderOutput(files=[], skipped=["a.py"],
                         summary="DECLINED: spans 4 large files, needs decomposition")
        assert _honest_empty_summary(co).startswith("DECLINED:")

    def test_phantom_gate_distills_to_lesson(self):
        key, lesson = distill("phantom", "empty patch")
        assert key == "phantom-patch"
        assert "phantom" in lesson.lower()


# ── Regression: 'complete' must never mean "nothing shipped" ──────────────────
# A gate retry (e.g. the pytest-feedback re-prompt) can overwrite coder_out with
# an empty patch; the old code fell through to an empty branch + a misleading
# status='complete' with files_changed=[]. The backstop turns that into a clean
# no_change and creates NO branch. Reproduced live when auto-diff tripped the
# pytest retry on a 1200-line file.
class TestEmptyPatchNeverCompletes:
    def test_pytest_retry_emptying_patch_yields_no_change_not_empty_complete(
        self, monkeypatch,
    ):
        from app.schemas.api_schemas import ActuateRequest, FindingPayload
        from app.services.coder_orchestrator import CoderOrchestrator

        # Self-repo + pytest gate ON so the gate path (and its retry) runs.
        monkeypatch.setattr(co_mod, "_SELF_REPO", "o/r")
        monkeypatch.setattr(co_mod, "_PYTEST_GATE_ENABLED", True)

        # GitHub deps: a tiny tree, current content for the one target file.
        async def _tree(*a, **k):
            return ["h.py"]
        async def _contents(*a, **k):
            return {"h.py": "def f():\n    return 1\n"}
        monkeypatch.setattr(co_mod.GitHubAPIService, "get_file_tree", _tree)
        monkeypatch.setattr(co_mod, "_fetch_current_contents", _contents)
        monkeypatch.setattr(co_mod, "_resolve_target_paths", lambda *a, **k: ["h.py"])

        # Verify-loop: first candidate passes the cheap checks unchanged.
        from app.services import verify_loop as vl
        async def _vloop(req, brief, agent, co, *a, **k):
            return co, vl.VerifyLoopResult(co, True, 0, [], "clean", [], [])
        monkeypatch.setattr(
            CoderOrchestrator, "_run_verify_loop", classmethod(
                lambda c, *a, **k: _vloop(*a, **k)),
        )

        # First Coder call returns a real 1-file patch; the pytest gate fails;
        # the retry (run_with_lint_feedback) returns an EMPTY patch.
        good = CoderOutput(
            files=[CoderFile(path="h.py", new_content="def f():\n    return 2\n",
                             rationale="bump")],
            summary="changes the return value of f from 1 to 2 for the finding",
        )
        empty = CoderOutput(files=[], skipped=["h.py"],
                            summary="DECLINED: cannot fix without breaking tests")

        class _Agent:
            def run(self, *a, **k):
                return good
            def run_with_lint_feedback(self, *a, **k):
                return empty
        monkeypatch.setattr(co_mod, "CoderAgent", lambda *a, **k: _Agent())

        # pytest gate always reports a regression → forces the retry path.
        class _Res:
            passed = False
            reason = "1 test failed"
            before = 10
            after = 9
        monkeypatch.setattr(co_mod.vg, "gate_patch", lambda *a, **k: (_Res(), {}))
        monkeypatch.setattr(co_mod.vg, "restore_snapshot", lambda *a, **k: None)
        monkeypatch.setattr(co_mod.vg, "update_baseline", lambda *a, **k: None)
        monkeypatch.setattr(co_mod.ir, "claim_path", lambda *a, **k: True)
        monkeypatch.setattr(co_mod.ir, "release_path", lambda *a, **k: None)

        # A branch must NOT be created. Fail loudly if it is.
        async def _boom(*a, **k):
            raise AssertionError("create_branch called for an empty patch")
        monkeypatch.setattr(co_mod.GitHubPRService, "create_branch", _boom)

        req = ActuateRequest(
            owner="o", repo="r", branch="main", access_token="t", open_pr=False,
            finding=FindingPayload(kind="guardrail", id="x", title="t",
                                   description="d", file="h.py"),
        )
        resp = asyncio.run(CoderOrchestrator.run_actuation(req))
        # With the empty-retry fix, an empty retry KEEPS the original rejection
        # (pytest_rejected) instead of falling through to an empty 'complete'.
        # The _boom guard above proves no branch was created. The core invariant:
        # never a 'complete' with nothing shipped.
        assert resp.status == "pytest_rejected"
        assert resp.files_changed == []
        assert not (resp.status == "complete" and not resp.files_changed)


def _await_value(v):
    async def _a():
        return v
    return _a()
