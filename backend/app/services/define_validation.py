"""
define_validation — emit a ValidationSpec for a finding (Phase 5 EvalOps).

The Planner side of EvalOps: the analog of how the `write-test` skill turns a
finding into a test, this turns a feature/milestone finding into a declarative
ValidationSpec (scenarios + metric/log expectations) describing what "this
feature works" MEANS, executable by eval_runner.

`define_spec(finding, provider, ...)` makes one structured-output call (the
provider is injected, same contract as the agents) forcing a ValidationSpec.
Fail-open: if the provider is unavailable or returns nothing usable, it
synthesizes a MINIMAL spec from the finding (a health/smoke scenario) so the
eval path still has *a* spec rather than raising — flagged via is_empty so the
caller treats "no real spec" as "no signal", not "passed".

Kept separate from the Coder skill registry: skills shape how the CODER writes
a patch; this shapes what the PLANNER asserts about the result. They compose
(the Coder builds the feature, this validates it) but are different surfaces.
"""
from __future__ import annotations

import logging
from typing import Any, List, Optional

from app.schemas.api_schemas import FindingPayload
from app.services.eval_schemas import Scenario, ValidationSpec

logger = logging.getLogger("shipmate.define_validation")

_SYSTEM_PROMPT = (
    "You are a senior engineer defining the ACCEPTANCE TEST for a single "
    "shipped change on a FastAPI backend, as a declarative ValidationSpec the "
    "platform will execute against the running app. You do NOT write code — you "
    "describe how to PROVE the change works.\n\n"
    "Produce a ValidationSpec:\n"
    "  • scenarios: HTTP requests against the booted app (relative URLs like "
    "'/health'), each with the required status and concrete assertions on "
    "the JSON response (dotted paths like 'readiness_score' or 'items.0.id', "
    "with equals/contains/gte/lte/exists).\n"
    "  • metrics: names of counters/events that MUST fire during the scenario "
    "(only if the change is supposed to emit one — otherwise leave empty).\n"
    "  • logs: patterns that must NOT appear (defaults to ERROR/Traceback).\n\n"
    "HARD RULES:\n"
    "  • Assert REAL, observable behaviour of THIS change — not generic '200 "
    "OK'. If the change adds a field, assert that field exists with the right "
    "value. If you can't predict a concrete response shape, prefer a narrow "
    "scenario (status + one existence assertion) over a vacuous one.\n"
    "  • Use only endpoints that plausibly exist; a health check ('/health' "
    "or '/') is always safe as a baseline scenario.\n"
    "  • Do NOT assert on a 4xx/5xx as success. Pick the ONE correct status.\n"
    "Return ONLY the structured ValidationSpec object."
)


def _slug(text: str, n: int = 40) -> str:
    import re
    s = re.sub(r"[^a-zA-Z0-9]+", "-", (text or "").lower()).strip("-")
    return s[:n] or "spec"


def _minimal_spec(finding: FindingPayload) -> ValidationSpec:
    """Fallback spec when no provider / no usable model output: a baseline
    health-endpoint scenario. is_empty() is False (it has a scenario), but it
    asserts only liveness — the caller can decide that a minimal spec is a weak
    signal. Kept deliberately conservative so it never false-fails a good patch."""
    return ValidationSpec(
        name=f"min-{_slug(finding.title)}",
        description=(
            f"Minimal baseline validation for {finding.kind}/{finding.id}: the "
            f"app still boots and serves health after the change."
        ),
        scenarios=[
            Scenario(name="health", method="GET", url="/health", expect_status=200),
        ],
    )


def _build_user_prompt(finding: FindingPayload) -> str:
    parts = [
        f"# Change to validate\n[{finding.kind.upper()}] {finding.title}",
    ]
    if finding.description:
        parts.append(f"Description: {finding.description}")
    if getattr(finding, "recommendation", ""):
        parts.append(f"Approach: {finding.recommendation}")
    if getattr(finding, "file", ""):
        parts.append(f"Primary file: {finding.file}")
    parts.append(
        "Define the ValidationSpec that proves this change works against the "
        "running app. Include a health baseline scenario plus any scenario that "
        "directly exercises the change. Return ONLY the ValidationSpec."
    )
    return "\n\n".join(parts)


def define_spec(
    finding: FindingPayload,
    provider: Optional[Any] = None,
    *,
    deployment_hint: str = "smart",
) -> ValidationSpec:
    """Produce a ValidationSpec for `finding`. When `provider` is given, make one
    structured-output call; otherwise (or on any failure) return the minimal
    baseline spec. Always returns a spec — never raises."""
    if provider is None:
        return _minimal_spec(finding)
    try:
        spec = provider.invoke_structured_sync(
            system_prompt=_SYSTEM_PROMPT,
            user_prompt=_build_user_prompt(finding),
            schema_class=ValidationSpec,
            deployment_hint=deployment_hint,
        )
    except Exception as e:
        logger.info("define_spec provider call failed (%s) — minimal spec", e)
        return _minimal_spec(finding)
    if not isinstance(spec, ValidationSpec) or spec.is_empty():
        logger.info("define_spec produced an empty spec — using minimal baseline")
        return _minimal_spec(finding)
    # Always ensure a health baseline is present so a feature spec that forgot
    # liveness still proves the app boots.
    if not any(s.url in ("/health", "/") for s in spec.scenarios):
        spec.scenarios.insert(
            0, Scenario(name="health", method="GET", url="/health", expect_status=200),
        )
    return spec
