"""
EvalOps schemas (Phase 5) — the declarative ValidationSpec + its report.

The insight behind Phase 5: `test_synthesizer` is already baby EvalOps — it
asks "did this artifact move a measurable signal (coverage delta) in the right
direction, run in a sandbox?" and accepts/rejects on that. EvalOps generalizes
the SIGNAL from one hard-coded coverage rule to a declarative spec:

    coverage_delta > 0                  →  metric X fired
                          generalizes      AND scenario Y passed (HTTP + JSON)
                                            AND logs clean

A ValidationSpec is what a feature finding's validation looks like, the same
way a `write-test` skill emits a test. It's executed in the Phase-2 sandbox
(eval_runner) and produces an EvalReport the accept/reject logic consumes.

All models are plain Pydantic so a provider can emit a ValidationSpec directly
(like CoderOutput) and the report serializes to eval-report.json for CI / the
CIWatcher to consume.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from pydantic import BaseModel, Field


# ── Scenario: an HTTP call + assertions on the response ──────────────────────

class Assertion(BaseModel):
    """One assertion over a JSON response, addressed by a JSONPath-lite path
    (dotted, with integer indices: `agents.repo_lens.file_count`, `items.0.id`).
    Exactly one operator should be set; `exists` is the default check."""
    path: str = Field(..., description="Dotted path into the response JSON, e.g. 'readiness_score' or 'items.0.name'.")
    equals: Optional[Any] = Field(default=None, description="Value the path must equal.")
    contains: Optional[str] = Field(default=None, description="Substring the (stringified) value must contain.")
    gte: Optional[float] = Field(default=None, description="Value must be >= this number.")
    lte: Optional[float] = Field(default=None, description="Value must be <= this number.")
    exists: bool = Field(default=True, description="The path must resolve (default check when no operator set).")


class Scenario(BaseModel):
    """A single request to fire against the booted app + what the response must
    look like. URL is relative (the harness prefixes the TestClient base)."""
    name: str
    method: str = "GET"
    url: str = Field(..., description="Relative path, e.g. '/api/health'.")
    json_body: Optional[Dict[str, Any]] = Field(default=None, description="JSON request body (POST/PUT).")
    headers: Dict[str, str] = Field(default_factory=dict)
    expect_status: int = Field(default=200, description="Required HTTP status code.")
    assertions: List[Assertion] = Field(default_factory=list)


class MetricExpectation(BaseModel):
    """A metric (counter/event) that MUST have been emitted to the metrics sink
    during the scenario run (the eval harness points the sink at an isolated
    file and checks which names fired)."""
    name: str
    must_fire: bool = True


class LogExpectation(BaseModel):
    """Patterns that must NOT appear in the app's logs during the run (default:
    error/traceback). 'logs clean' is the cheapest real signal — a 200 response
    while the server logged a swallowed traceback is a silent failure."""
    forbid_patterns: List[str] = Field(
        default_factory=lambda: ["ERROR", "Traceback", "CRITICAL"]
    )


class ValidationSpec(BaseModel):
    """The full declarative validation for a finding. Emitted by the Planner's
    define-validation skill; executed by eval_runner in the sandbox."""
    name: str = Field(..., description="Short identifier, e.g. 'health-endpoint-up'.")
    description: str = ""
    scenarios: List[Scenario] = Field(default_factory=list)
    metrics: List[MetricExpectation] = Field(default_factory=list)
    logs: LogExpectation = Field(default_factory=LogExpectation)
    # Free-form env the harness exports before booting (e.g. feature flags).
    environment: Dict[str, str] = Field(default_factory=dict)

    def is_empty(self) -> bool:
        """A spec with nothing to check is a no-op — the runner reports it as
        a (vacuous) pass but the synthesizer treats it as 'no signal'."""
        return not self.scenarios and not self.metrics


# ── Report: the structured outcome eval_runner produces ──────────────────────

class ScenarioResult(BaseModel):
    name: str
    passed: bool
    status_code: Optional[int] = None
    failures: List[str] = Field(default_factory=list)


class MetricResult(BaseModel):
    name: str
    fired: bool
    satisfied: bool   # fired matches must_fire


class EvalReport(BaseModel):
    """What eval_runner writes to eval-report.json. `passed` is the AND of every
    scenario passing, every metric expectation satisfied, and logs clean."""
    spec_name: str
    passed: bool
    scenarios: List[ScenarioResult] = Field(default_factory=list)
    metrics: List[MetricResult] = Field(default_factory=list)
    log_clean: bool = True
    log_violations: List[str] = Field(default_factory=list)
    error: Optional[str] = Field(default=None, description="Set when the eval could not run (boot failure, timeout).")

    @property
    def ran(self) -> bool:
        return self.error is None
