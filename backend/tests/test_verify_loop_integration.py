"""Orchestrator integration of the Phase 4 verify-loop + its review fixes.

Drives CoderOrchestrator._run_verify_loop directly (no GitHub, no Bedrock — the
agent + checks are stubbed) to pin the behaviours the adversarial review flagged:
  • a scope failure is classified via failed_checks (→ scope_rejected), not by
    substring-sniffing the issue text;
  • the fail-open path still runs the PURE lint+scope checks (a hallucinated
    import is rejected, never shipped unverified) — it does NOT return passed=True
    blindly;
  • the smoke (execution) step is gated on SHIPMATE_PYTEST_GATE, not just self-repo.
"""
import asyncio

import pytest

from app.agents.coder_agent import CoderBrief, CoderOutput, CoderFile
from app.services.coder_orchestrator import CoderOrchestrator, _SELF_REPO


class _Finding:
    def __init__(self, kind="guardrail", category="injection"):
        self.kind = kind
        self.id = "SEC-1"
        self.category = category
        self.severity = "high"


class _Req:
    def __init__(self):
        owner, repo = _SELF_REPO.split("/")
        self.owner = owner
        self.repo = repo
        self.branch = "main"
        self.finding = _Finding()


class _Agent:
    """Stub Coder: returns a fixed (clean) patch on feedback."""
    def __init__(self, fixed=None):
        self.calls = 0
        self._fixed = fixed

    def run_with_lint_feedback(self, brief, issues, hint):
        self.calls += 1
        return self._fixed


def _co(path, content, summary="a sufficiently long detailed change summary"):
    return CoderOutput(
        files=[CoderFile(path=path, new_content=content, rationale="r")],
        summary=summary,
    )


async def _drive(req, coder_out, agent, target_files, file_tree):
    brief = CoderBrief(
        task="t", repo_full_name=f"{req.owner}/{req.repo}",
        target_files=target_files, finding_kind=req.finding.kind,
        finding_id=req.finding.id, finding_category=req.finding.category,
    )
    events = []
    async def emit(stage, **f):
        events.append(stage)
    return await CoderOrchestrator._run_verify_loop(
        req, brief, agent, coder_out, target_files, file_tree, emit,
    )


def test_scope_failure_tagged_for_classification(monkeypatch):
    # A patch that mass-deletes a file → scope check fails with the catch-all
    # message (no DROPS/top-level keyword). failed_checks must name 'scope' so
    # the orchestrator returns scope_rejected (not lint_rejected).
    req = _Req()
    original = "\n".join(f"line {i}" for i in range(50))
    target_files = {"README.md": original}
    bad = _co("README.md", "line 0\n")  # deletes ~49/50 lines
    agent = _Agent(fixed=None)  # no fix → loop can't converge

    out, loop = asyncio.run(_drive(req, bad, agent, target_files, ["README.md"]))
    assert loop.passed is False
    assert "scope" in loop.failed_checks, loop.failed_checks


def test_fail_open_still_runs_pure_checks(monkeypatch):
    # Force the loop to ERROR, then assert the fallback still REJECTS a
    # hallucinated-import patch via the pure lint check (not passed=True).
    import app.services.verify_loop as vl
    monkeypatch.setattr(vl, "run_verify_loop",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("loop boom")))

    req = _Req()
    target_files = {"backend/app/x.py": "y = 1\n"}
    bad = _co("backend/app/x.py", "from app.services.ghost_module import z\ny = 1\n")
    agent = _Agent()

    out, loop = asyncio.run(_drive(req, bad, agent, target_files, ["backend/app/x.py"]))
    # The loop errored, but the pure lint+scope fallback caught the bad import.
    assert loop.passed is False, "fail-open must NOT ship an unlinted patch"
    assert loop.stop_reason == "setup_error"
    assert any("ghost_module" in i or "hallucinated" in i.lower()
               or "not in repo" in i.lower() for i in loop.issues)


def test_fail_open_passes_a_clean_patch(monkeypatch):
    import app.services.verify_loop as vl
    monkeypatch.setattr(vl, "run_verify_loop",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    req = _Req()
    target_files = {"backend/app/x.py": "y = 1\n"}
    clean = _co("backend/app/x.py", "y = 2\n")
    out, loop = asyncio.run(_drive(req, clean, _Agent(), target_files, ["backend/app/x.py"]))
    # Clean patch + errored loop → pure fallback passes it through.
    assert loop.passed is True


def test_smoke_gated_on_pytest_gate(monkeypatch):
    # With SHIPMATE_PYTEST_GATE disabled, the loop must NOT include the smoke
    # (execution) check — only the pure lint+scope. We assert by spying on
    # default_checks' include_smoke argument.
    import app.services.coder_orchestrator as co
    monkeypatch.setattr(co, "_PYTEST_GATE_ENABLED", False)

    seen = {}
    import app.services.verify_checks as vc
    real = vc.default_checks

    def spy(kind, category, tf, ft, include_smoke=True):
        seen["include_smoke"] = include_smoke
        return real(kind, category, tf, ft, include_smoke=include_smoke)

    monkeypatch.setattr(vc, "default_checks", spy)

    req = _Req()
    clean = _co("backend/app/x.py", "y = 2\n")
    asyncio.run(_drive(req, clean, _Agent(), {"backend/app/x.py": "y=1\n"}, ["backend/app/x.py"]))
    assert seen["include_smoke"] is False
