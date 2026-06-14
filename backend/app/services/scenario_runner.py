"""
scenario_runner — the pure assertion engine for EvalOps (Phase 5).

Given a Scenario's expected response (status + assertions) and the ACTUAL
response (status + parsed JSON), decide pass/fail and produce human-readable
failures. Plus the metric-expectation and log-clean evaluators. Everything here
is PURE (no HTTP, no subprocess) — eval_runner does the booting/firing and feeds
the results in, so this module is trivially unit-testable.

The path language is JSONPath-LITE: dotted segments, integer segments index
lists. `agents.repo_lens.file_count`, `items.0.id`. Deliberately tiny — a real
JSONPath dependency isn't worth it for the assertions a ValidationSpec needs,
and a small resolver has no surprising behaviour.
"""
from __future__ import annotations

import logging
import re
from typing import Any, List, Optional, Tuple

from app.services.eval_schemas import (
    Assertion, EvalReport, LogExpectation, MetricExpectation, MetricResult,
    Scenario, ScenarioResult, ValidationSpec,
)

logger = logging.getLogger("shipmate.scenario_runner")

_MISSING = object()


def resolve_path(data: Any, path: str) -> Any:
    """Resolve a dotted JSONPath-lite into `data`. Integer segments index lists.
    Returns the sentinel _MISSING when any segment doesn't resolve (so an
    explicit None value is distinguishable from absence)."""
    cur = data
    if not path:
        return cur
    for seg in path.split("."):
        if isinstance(cur, dict):
            if seg not in cur:
                return _MISSING
            cur = cur[seg]
        elif isinstance(cur, (list, tuple)):
            if not re.fullmatch(r"-?\d+", seg):
                return _MISSING
            idx = int(seg)
            if not (-len(cur) <= idx < len(cur)):
                return _MISSING
            cur = cur[idx]
        else:
            return _MISSING
    return cur


def eval_assertion(data: Any, a: Assertion) -> Optional[str]:
    """Evaluate one assertion against the response `data`. Returns None on pass,
    or a human-readable failure string. Operator precedence: equals → contains →
    gte/lte → exists (the default)."""
    value = resolve_path(data, a.path)
    if value is _MISSING:
        # Any operator on a missing path fails; an exists-check fails too.
        return f"path '{a.path}' did not resolve in the response"

    if a.equals is not None:
        if value != a.equals:
            return f"'{a.path}' = {value!r}, expected == {a.equals!r}"
        return None
    if a.contains is not None:
        if a.contains not in str(value):
            return f"'{a.path}' = {value!r} does not contain {a.contains!r}"
        return None
    if a.gte is not None or a.lte is not None:
        if not isinstance(value, (int, float)) or isinstance(value, bool):
            return f"'{a.path}' = {value!r} is not numeric (gte/lte)"
        if a.gte is not None and value < a.gte:
            return f"'{a.path}' = {value} is below gte {a.gte}"
        if a.lte is not None and value > a.lte:
            return f"'{a.path}' = {value} is above lte {a.lte}"
        return None
    # Default: existence (already resolved above, so it exists).
    return None


def eval_scenario(
    scenario: Scenario,
    actual_status: int,
    actual_json: Any,
) -> ScenarioResult:
    """Compare a scenario's expectations against an actual response. Pure."""
    failures: List[str] = []
    if actual_status != scenario.expect_status:
        failures.append(
            f"status {actual_status}, expected {scenario.expect_status}"
        )
    # Only evaluate body assertions when we got the expected status — a wrong
    # status already explains the failure, and the body is likely an error blob.
    if actual_status == scenario.expect_status:
        for a in scenario.assertions:
            f = eval_assertion(actual_json, a)
            if f:
                failures.append(f)
    return ScenarioResult(
        name=scenario.name, passed=not failures,
        status_code=actual_status, failures=failures,
    )


def eval_metrics(
    expectations: List[MetricExpectation],
    fired: set,
) -> List[MetricResult]:
    """Check each metric expectation against the set of names that fired."""
    results: List[MetricResult] = []
    for m in expectations:
        did_fire = m.name in fired
        satisfied = did_fire if m.must_fire else not did_fire
        results.append(MetricResult(name=m.name, fired=did_fire, satisfied=satisfied))
    return results


def eval_logs(log_text: str, expectation: LogExpectation) -> Tuple[bool, List[str]]:
    """Return (clean, violations). A log line containing any forbidden pattern
    is a violation. Case-sensitive (patterns like 'ERROR'/'Traceback' are
    already the canonical casing)."""
    violations: List[str] = []
    for pat in expectation.forbid_patterns:
        for line in (log_text or "").splitlines():
            if pat in line:
                violations.append(f"forbidden log pattern {pat!r}: {line.strip()[:160]}")
                break  # one example per pattern is enough
    return (not violations), violations


def assemble_report(
    spec: ValidationSpec,
    scenario_results: List[ScenarioResult],
    metric_results: List[MetricResult],
    log_clean: bool,
    log_violations: List[str],
    error: Optional[str] = None,
) -> EvalReport:
    """Combine the parts into an EvalReport. `passed` = every scenario passed
    AND every metric expectation satisfied AND logs clean AND no run error.
    An empty spec (nothing to check) reports passed=True but the caller treats
    that as 'no signal' (see eval_runner / the synthesizer integration)."""
    passed = (
        error is None
        and all(s.passed for s in scenario_results)
        and all(m.satisfied for m in metric_results)
        and log_clean
    )
    return EvalReport(
        spec_name=spec.name,
        passed=passed,
        scenarios=scenario_results,
        metrics=metric_results,
        log_clean=log_clean,
        log_violations=log_violations,
        error=error,
    )
