"""
test_synthesizer — closes the TestPilot loop: suggestion → real, VERIFIED test (A1).

Before this, TestPilot emitted `suggested_tests` (name + rationale) as PROSE.
Nothing executed them: the agent that's supposed to be ShipMate's testing brain
produced text, not test artifacts. "We suggest 12 tests" never became "we added
3 tests that demonstrably cover the gap".

This service is the missing sub-pipeline:

  SuggestedTest  →  synthetic `test` FindingPayload  →  CoderAgent writes the
  real test file  →  ValidationGate proves it is a GENUINE artifact:
      1. it is COLLECTABLE + PASSES on the current code (a test that errors on
         collection or fails outright is theater, not coverage);
      2. it ADDS coverage / a passing test (CoverageDelta.improved) — a test
         that exercises nothing new isn't worth committing.

Only a test that clears both becomes a `SynthesisResult(accepted=True)` the
caller (the autonomous loop / a future endpoint) can turn into a PR via the
normal CoderOrchestrator path. A test that doesn't is reported with the reason,
so the loop learns instead of opening a no-value PR.

The Coder call is injected (`coder_fn`) so this orchestration is unit-testable
with no Bedrock — the tests pass a stub that returns a known test file.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional

from app.schemas.agent_schemas import SuggestedTest
from app.schemas.api_schemas import FindingPayload
from app.services import validation_gate as vg

logger = logging.getLogger("shipmate.test_synthesizer")


@dataclass
class SynthesisResult:
    """Outcome of synthesizing + verifying one test suggestion."""
    suggestion_name: str
    accepted: bool
    reason: str
    test_path: Optional[str] = None
    files: List[Dict[str, str]] = field(default_factory=list)   # [{path,new_content,rationale}]
    coverage_delta: Optional[Dict[str, Any]] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "suggestion": self.suggestion_name,
            "accepted": self.accepted,
            "reason": self.reason,
            "test_path": self.test_path,
            "coverage_delta": self.coverage_delta,
        }


def suggestion_to_finding(test: SuggestedTest) -> FindingPayload:
    """Turn a TestPilot SuggestedTest into the `test`-kind FindingPayload the
    CoderOrchestrator already knows how to actuate. The target production file
    (if the suggestion named one) rides on `file` so Coder sees the real code
    under test and the orchestrator's `_resolve_target_paths` test-branch adds a
    sibling test file to write into."""
    desc = (
        f"{test.description}\n\n"
        f"Write a REAL, executable test named `{test.name}` ({test.type}). It "
        f"must import and exercise the actual production code, assert one "
        f"concrete expected outcome (no theater, no `assert True`, no accepting "
        f"4xx as success), and FAIL if that behaviour regressed."
    )
    return FindingPayload(
        kind="test",
        id=f"synth-{_slug(test.name)}",
        title=test.name,
        description=desc,
        recommendation=getattr(test, "rationale", "") or "",
        file=test.target_file,
        category="testing",
    )


def _slug(text: str, n: int = 40) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n] or "case"


# A Coder function: (finding) -> list of {path, new_content, rationale} dicts.
CoderFn = Callable[[FindingPayload], List[Dict[str, str]]]


def synthesize_and_verify(
    test: SuggestedTest,
    coder_fn: CoderFn,
    *,
    cov_package: str = "app",
    require_coverage_gain: bool = True,
    self_repo_only: bool = True,
    eval_spec: Optional[Any] = None,
    eval_fn: Optional[Callable[[Any, List[Dict[str, str]]], Any]] = None,
) -> SynthesisResult:
    """Synthesize a test from `test` via `coder_fn`, then VERIFY it is a genuine
    artifact (collectable + passing + coverage-adding) using ValidationGate.

    `coder_fn` is injected (the orchestrator passes a real CoderAgent-backed
    closure; tests pass a stub) and returns the produced files as dicts.

    `require_coverage_gain`: when True, a test that passes but doesn't measurably
    raise coverage or the passing-test count is REJECTED (no-value artifact).
    Set False to accept any green, collectable test.

    EvalOps generalization (Phase 5): when `eval_spec` (a ValidationSpec) and
    `eval_fn` ((spec, files) -> EvalReport) are supplied, the coverage signal is
    AUGMENTED with the eval signal — the artifact must ALSO make the spec's
    scenarios pass / metrics fire / logs stay clean. This is the same accept/
    reject shape with a pluggable signal: 'did the artifact move a measurable
    signal in the right direction?' generalized from coverage-delta to a
    declarative spec. When neither is supplied, behaviour is exactly as before.

    Returns a SynthesisResult; the working tree is always restored (we never
    leave the synthesized file on disk — a PR is opened separately via the
    normal actuate path if accepted)."""
    finding = suggestion_to_finding(test)

    # 1. Ask the Coder for the test file(s).
    try:
        files = coder_fn(finding)
    except Exception as e:
        return SynthesisResult(test.name, False, f"coder failed: {e}")

    files = [f for f in (files or []) if f.get("path") and f.get("new_content")]
    test_files = [f for f in files if _looks_like_test_path(f["path"])]
    if not test_files:
        return SynthesisResult(
            test.name, False,
            "coder produced no test file (only non-test paths or nothing)",
            files=files,
        )
    test_path = test_files[0]["path"]

    # 2. Verify via the differential gate: measure coverage before, apply, after.
    #    measure_coverage_delta restores nothing — WE restore in finally.
    delta, snap = vg.measure_coverage_delta(files, cov_package=cov_package)
    try:
        # 2a. The patched suite must still be green (no regression introduced by
        #     the new test — a synthesized test that breaks others is rejected).
        if delta.regressed:
            return SynthesisResult(
                test.name, False,
                f"synthesized test regressed the suite "
                f"({delta.before.passed} → {delta.after.passed} passing)",
                test_path=test_path, files=files, coverage_delta=delta.as_dict(),
            )
        # 2b. It must have actually RUN (added at least the one passing test).
        if delta.tests_delta <= 0:
            return SynthesisResult(
                test.name, False,
                "synthesized test did not add a passing test (not collectable, "
                "errored, or was skipped)",
                test_path=test_path, files=files, coverage_delta=delta.as_dict(),
            )
        # 2c. Optionally require it to add coverage (a genuine gap-filler) — but
        #     accept a coverage-flat test if coverage couldn't be MEASURED
        #     (cov=-1), since "no signal" must not masquerade as "no value".
        if require_coverage_gain:
            measurable = delta.before.coverage_pct >= 0 and delta.after.coverage_pct >= 0
            if measurable and delta.coverage_delta <= 0:
                return SynthesisResult(
                    test.name, False,
                    f"synthesized test passes but adds no coverage "
                    f"(Δ{delta.coverage_delta:+}%) — likely retests covered code",
                    test_path=test_path, files=files, coverage_delta=delta.as_dict(),
                )
        # 2d. EvalOps signal (Phase 5, opt-in): the artifact must ALSO satisfy
        #     the declarative spec — scenarios pass, metrics fire, logs clean.
        #     Augments the coverage signal; a passing-but-spec-failing artifact
        #     is rejected with the eval failures so the loop learns.
        if eval_spec is not None and eval_fn is not None:
            try:
                report = eval_fn(eval_spec, files)
            except Exception as e:
                report = None
                logger.info("eval_fn raised (%s) — treating eval as no-signal", e)
            # Intentional asymmetry (review F7): a BOOT/infra failure
            # (report.ran False) is treated as NO SIGNAL here — it must not flip
            # a coverage-accepted test to rejected, because the eval infra
            # failing says nothing about the artifact. (The CI eval-gate, by
            # contrast, DOES red on a boot failure — there a broken boot IS the
            # signal.) Only a spec that actually RAN and FAILED rejects.
            if report is not None and report.ran and not report.passed:
                fails = [f for s in report.scenarios for f in s.failures]
                fails += [f"metric {m.name} expectation unmet" for m in report.metrics if not m.satisfied]
                fails += report.log_violations
                return SynthesisResult(
                    test.name, False,
                    f"test passes + adds coverage but the EvalOps spec FAILED: "
                    f"{'; '.join(fails[:4]) or 'see eval report'}",
                    test_path=test_path, files=files, coverage_delta=delta.as_dict(),
                )

        return SynthesisResult(
            test.name, True,
            f"verified: +{delta.tests_delta} passing test(s), "
            f"coverage Δ{delta.coverage_delta:+}%",
            test_path=test_path, files=files, coverage_delta=delta.as_dict(),
        )
    finally:
        # Always restore — the artifact ships via a PR (actuate), not by being
        # left on the local tree.
        vg.restore_snapshot(snap)


def synthesize_top(
    tests: List[SuggestedTest],
    coder_fn: CoderFn,
    *,
    max_attempts: int = 3,
    **kwargs,
) -> List[SynthesisResult]:
    """Try the top `max_attempts` suggestions in order, returning a result per
    attempt. Stops early once one is accepted (the caller usually wants ONE
    verified test to PR per round, like the loop picks one finding per kind)."""
    results: List[SynthesisResult] = []
    for t in (tests or [])[:max_attempts]:
        res = synthesize_and_verify(t, coder_fn, **kwargs)
        results.append(res)
        logger.info("test synthesis %s: %s", t.name,
                    "ACCEPTED" if res.accepted else f"rejected ({res.reason})")
        if res.accepted:
            break
    return results


def _looks_like_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith((".test.tsx", ".test.ts", ".spec.ts", ".spec.tsx", "_test.py"))
        or "/tests/" in path
        or "/__tests__/" in path
    )
