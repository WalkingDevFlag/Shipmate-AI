"""
Inner verify-loop (Phase 4) — iterate the Coder against CHEAP checks before
paying for the expensive full gate / PR.

The actuate path historically had THREE scattered one-shot feedback retries
(lint, scope, pytest), each independently re-prompting the Coder once. That's
three near-identical "rejected → run_with_lint_feedback once → re-check" blocks
with no shared budget and no notion of iterating more than once on a stubborn
patch. This module unifies the CHEAP front of that into a single principled,
bounded loop:

    generate ─▶ cheap-verify (lint + scope + sandbox smoke) ─▶ clean? ─▶ done
                     ▲                                          │
                     └──────── feed concrete errors back ◀──────┘  (≤ max_iters,
                                                                    ≤ token budget)

Why a loop, not the old one-shots:
  • A patch can fail lint on iter 1, get fixed, then fail scope on iter 2 — the
    old code only ever retried each gate ONCE in isolation. A loop catches the
    cascade.
  • The checks here are CHEAP (AST lint = pure; scope = text; smoke = a ~2s
    import) — so iterating is affordable. The EXPENSIVE full pytest gate stays
    OUTSIDE the loop, run once on the loop's surviving output (Phase 2).
  • It composes the prior phases: the brief already carries the repo map (P1),
    the smoke check runs in the sandbox HOME-safe env (P2), and the feedback
    re-prompt uses the skill-composed prompt (P3) via the injected coder fn.

Design for testability + safety:
  • Every side-effecting dependency is INJECTED — the check functions and the
    "re-run Coder with feedback" callback are parameters, so unit tests drive
    the loop with pure stubs (no Bedrock, no subprocess).
  • Bounded twice: a hard `max_iters` AND a token budget read from run_trace
    (so a long analyze run can't let the loop burn the whole budget). Either
    bound stopping is a clean, reported outcome — never an exception.
  • Fail-open: any unexpected error returns the BEST output so far with
    passed=False and a stop_reason, so the caller can fall back to its existing
    gates exactly as before.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Protocol

logger = logging.getLogger("shipmate.verify_loop")

# Default bounds. Iterations are small — the marginal fix rate drops fast, and
# each iter costs a full Coder round-trip. The token budget is a SECONDARY cap:
# when an analyze/actuate run is already token-heavy, the loop shortens itself.
_DEFAULT_MAX_ITERS = 2
# If fewer than this many approx tokens of headroom remain in the active run,
# don't start another Coder iteration (a Coder call is ~thousands of tokens).
_MIN_TOKEN_HEADROOM = 8000
# Soft ceiling on total approx tokens a single run may reach before the loop
# stops re-prompting. None ⇒ no token cap (iteration cap still applies).
_DEFAULT_TOKEN_BUDGET: Optional[int] = None


@dataclass
class CheckResult:
    """Outcome of ONE cheap check over a candidate patch.

    issues: human-readable problems (empty ⇒ this check passed). They're fed
    VERBATIM back to the Coder, so phrase them as actionable instructions.
    fatal:  when True, the loop stops immediately even if iters/budget remain
    (e.g. unparseable output that re-prompting won't fix in the cheap tier)."""
    name: str
    issues: List[str] = field(default_factory=list)
    fatal: bool = False

    @property
    def ok(self) -> bool:
        return not self.issues


# A check takes the current candidate (coder_out) + context and returns a
# CheckResult. Injected so tests use pure stubs and prod composes lint / scope /
# sandbox-smoke. coder_out is duck-typed (.files: list of objects with .path /
# .new_content, .summary) to avoid importing the agent schema here.
class Check(Protocol):
    def __call__(self, coder_out: object) -> CheckResult: ...


# Re-run the Coder with the accumulated issues fed back, returning a NEW
# coder_out (or None on failure). Injected so the loop never imports the agent
# or a provider directly.
CoderFeedbackFn = Callable[[List[str]], Optional[object]]


@dataclass
class VerifyLoopResult:
    """What the loop produced.

    coder_out:   the best (last) candidate — ALWAYS set (the loop never discards
                 the Coder's work; on failure the caller still has something to
                 inspect / fall through to its own gates with).
    passed:      True iff every cheap check was clean on the final candidate.
    iterations:  how many Coder re-runs happened (0 ⇒ first output already clean).
    issues:      the outstanding issues on the final candidate (empty iff passed).
    stop_reason: 'clean' | 'max_iters' | 'budget' | 'fatal' | 'coder_failed'.
    history:     per-iteration issue lists, for observability / lessons."""
    coder_out: object
    passed: bool
    iterations: int
    issues: List[str]
    stop_reason: str
    history: List[List[str]] = field(default_factory=list)
    # Names of the checks that still had issues on the FINAL candidate (e.g.
    # {'lint','scope','smoke'}). Lets the caller classify the rejection by WHICH
    # check failed instead of substring-sniffing the issue text — which missed
    # scope_guard's catch-all 'DELETES N of M lines' message.
    failed_checks: List[str] = field(default_factory=list)


def _run_checks(coder_out: object, checks: List[Check]) -> CheckResult:
    """Run all checks, aggregating their issues into ONE CheckResult. Stops
    aggregating further issues once a fatal check trips (but still reports it).
    A check that raises is treated as 'passed' for that check (fail-open — a
    broken checker must never block a patch), logged at debug.

    The aggregate's `name` is a comma-joined list of the FAILED check names, so
    the caller can classify the rejection by which check failed (e.g. 'scope')
    rather than sniffing issue text."""
    all_issues: List[str] = []
    failed_names: List[str] = []
    fatal = False
    for check in checks:
        try:
            res = check(coder_out)
        except Exception as e:  # pragma: no cover - defensive; a checker bug
            logger.debug("verify_loop: check %r raised %s — treating as pass", check, e)
            continue
        if res.issues:
            all_issues.extend(res.issues)
            failed_names.append(res.name)
        if res.fatal:
            fatal = True
    return CheckResult(name=",".join(failed_names), issues=all_issues, fatal=fatal)


def _token_headroom(token_budget: Optional[int]) -> Optional[int]:
    """Remaining approx-token headroom in the active run, or None when there's
    no budget to enforce (no active run, or token_budget is None). Fail-open:
    any error reading the trace returns None (no token gating)."""
    if token_budget is None:
        return None
    try:
        from app.services import run_trace
        run_id = run_trace.get_current_run()
        if not run_id:
            return None
        spent = run_trace.run_summary(run_id).get("approx_tokens", 0)
        return max(0, token_budget - int(spent))
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("verify_loop: token headroom read failed (%s)", e)
        return None


def run_verify_loop(
    coder_out: object,
    checks: List[Check],
    coder_feedback_fn: CoderFeedbackFn,
    *,
    max_iters: int = _DEFAULT_MAX_ITERS,
    token_budget: Optional[int] = _DEFAULT_TOKEN_BUDGET,
    min_token_headroom: int = _MIN_TOKEN_HEADROOM,
) -> VerifyLoopResult:
    """Iterate `coder_out` against `checks`, re-prompting via `coder_feedback_fn`
    until the patch is clean or a bound is hit.

    Args:
        coder_out:         the Coder's FIRST candidate (already generated).
        checks:            cheap checks to run each iteration (lint / scope /
                           sandbox-smoke). Injected.
        coder_feedback_fn: (issues) -> new coder_out | None. Injected; the loop
                           never calls the model or a provider directly.
        max_iters:         max Coder RE-RUNS (the first candidate is iter 0).
        token_budget:      soft approx-token ceiling for the active run; when
                           headroom < min_token_headroom the loop stops before
                           another Coder call. None ⇒ iteration cap only.
        min_token_headroom: required headroom to start one more iteration.

    Never raises — a failure path returns the best candidate with passed=False.
    """
    history: List[List[str]] = []
    current = coder_out
    iterations = 0

    # First evaluation of the already-generated candidate.
    agg = _run_checks(current, checks)
    history.append(list(agg.issues))

    def _failed(agg: CheckResult) -> List[str]:
        return [n for n in agg.name.split(",") if n]

    while agg.issues:
        if agg.fatal:
            logger.info("verify_loop: fatal issue — stopping (%s)", agg.issues[:1])
            return VerifyLoopResult(current, False, iterations, agg.issues, "fatal", history, _failed(agg))
        if iterations >= max_iters:
            return VerifyLoopResult(current, False, iterations, agg.issues, "max_iters", history, _failed(agg))
        headroom = _token_headroom(token_budget)
        if headroom is not None and headroom < min_token_headroom:
            logger.info("verify_loop: token budget exhausted (headroom %d) — stopping", headroom)
            return VerifyLoopResult(current, False, iterations, agg.issues, "budget", history, _failed(agg))

        # Re-run the Coder with the concrete issues fed back.
        logger.info(
            "verify_loop: iter %d — %d issue(s), re-prompting Coder",
            iterations + 1, len(agg.issues),
        )
        new_out = None
        try:
            new_out = coder_feedback_fn(list(agg.issues))
        except Exception as e:
            logger.warning("verify_loop: coder_feedback_fn raised %s — stopping", e)
            return VerifyLoopResult(current, False, iterations, agg.issues, "coder_failed", history, _failed(agg))
        iterations += 1
        if new_out is None or not getattr(new_out, "files", None):
            # The Coder produced nothing usable — keep the prior candidate (it's
            # at least a real patch the caller's own gates can judge).
            return VerifyLoopResult(current, False, iterations, agg.issues, "coder_failed", history, _failed(agg))

        current = new_out
        agg = _run_checks(current, checks)
        history.append(list(agg.issues))

    return VerifyLoopResult(current, True, iterations, [], "clean", history, [])
