"""Phase-1B tests: opportunity verify critic, PlannerAgent, PlanCritic, and the
/api/build/execute route (plan-only + execute paths). The LLM is mocked
throughout so these run offline and deterministically."""
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "shipmate_1b_test.db")
os.environ["SHIPMATE_INFLIGHT_DB"] = _TMP_DB

from app.schemas.agent_schemas import (  # noqa: E402
    Opportunity, ExecutionPlan, BuildStep,
)
from app.services import opportunity_critic as oc  # noqa: E402
from app.services import plan_critic as pc  # noqa: E402
from app.agents.planner_agent import PlannerAgent  # noqa: E402


def _opp(title="Add a thing", **kw):
    return Opportunity(
        id=kw.get("id", "OPP-001"), title=title,
        category=kw.get("category", "improvement"),
        description=kw.get("description", "d"), impact=kw.get("impact", "better"),
        effort=kw.get("effort", "M"), estimated_days=kw.get("days", 3),
        target_files=kw.get("target_files", ["backend/app/main.py"]),
        evidence=kw.get("evidence", ["backend/app/main.py"]),
        suggested_approach=kw.get("approach", ["do x", "do y"]),
        rationale="r", source="discovery",
    )


# ── Fake provider ────────────────────────────────────────────────────────────

class _FakeProvider:
    """Returns a canned object per schema_class. Configure via the maps."""
    def __init__(self, verdict_map=None, planner_out=None, plan_verdict=None):
        self.verdict_map = verdict_map or {}
        self.planner_out = planner_out
        self.plan_verdict = plan_verdict

    def invoke_structured_sync(self, *, system_prompt, user_prompt, schema_class, deployment_hint="smart"):
        name = schema_class.__name__
        if name == "_OppCriticReport":
            from app.services.opportunity_critic import _OppVerdict, _OppCriticReport
            verdicts = [_OppVerdict(title=t, worth_doing=w, reason="r")
                        for t, w in self.verdict_map.items()]
            return _OppCriticReport(verdicts=verdicts)
        if name == "_PlannerOutput":
            return self.planner_out
        if name == "_PlanVerdict":
            return self.plan_verdict
        raise AssertionError(f"unexpected schema {name}")


# ── Opportunity verify (the OPP-007 "already built" case) ────────────────────

class TestVerify:
    def test_refutes_already_built(self):
        opps = [_opp("Persist reports for history"), _opp("Add a brand new endpoint", id="OPP-002")]
        provider = _FakeProvider(verdict_map={
            "persist reports for history": False,   # already built
            "add a brand new endpoint": True,
        })
        kept = oc.verify_opportunities(opps, code_blob="some real code", provider=provider)
        titles = [o.title for o in kept]
        assert "Persist reports for history" not in titles
        assert "Add a brand new endpoint" in titles

    def test_failopen_no_provider(self):
        opps = [_opp("X")]
        assert oc.verify_opportunities(opps, "code", provider=None) == opps

    def test_failopen_no_code(self):
        opps = [_opp("X")]
        assert oc.verify_opportunities(opps, "", provider=_FakeProvider()) == opps

    def test_unknown_titles_kept(self):
        # Critic only refutes what it explicitly marks False; silence ⇒ keep.
        opps = [_opp("Mentioned"), _opp("Not mentioned", id="OPP-002")]
        provider = _FakeProvider(verdict_map={"mentioned": True})
        kept = oc.verify_opportunities(opps, "code", provider)
        assert len(kept) == 2


# ── Deterministic already-built prefilter (the OPP-007 fix) ──────────────────

class TestAlreadyBuilt:
    _TREE = [
        "backend/app/api/routes/analysis.py",
        "backend/app/services/report_store.py",
        "frontend/src/lib/api.ts",
    ]
    _KEY = {
        # The /history route lives here — OUTSIDE the discoverer's blob window,
        # which is exactly why the LLM critic missed it.
        "backend/app/api/routes/analysis.py":
            '@router.get("/repos/{owner}/{repo}/history")\nasync def repo_history(): ...',
    }

    def test_refutes_proposed_existing_route(self):
        opp = _opp("Add analysis history endpoint",
                   description="Add a GET /api/repos/{owner}/{repo}/history endpoint to persist reports",
                   id="OPP-006")
        reason = oc.already_built(opp, self._TREE, self._KEY)
        assert reason is not None
        assert "history" in reason

    def test_refutes_proposed_existing_file(self):
        opp = _opp("Add report persistence store",
                   description="Create report_store.py to persist analysis reports",
                   id="OPP-007")
        reason = oc.already_built(opp, self._TREE, self._KEY)
        assert reason is not None
        assert "report_store.py" in reason

    def test_keeps_genuine_new_route(self):
        opp = _opp("Add a diff-preview endpoint",
                   description="Add a POST /api/build/diff endpoint to preview a patch",
                   id="OPP-010")
        assert oc.already_built(opp, self._TREE, self._KEY) is None

    def test_improve_existing_not_flagged(self):
        # 'Improve' (not 'add') a file that exists is legit work, not already-built.
        opp = _opp("Cache the file tree in api.ts",
                   description="Improve frontend/src/lib/api.ts to cache responses",
                   id="OPP-011", target_files=["frontend/src/lib/api.ts"])
        assert oc.already_built(opp, self._TREE, self._KEY) is None

    def test_filter_drops_and_annotates(self):
        built = _opp("Add /repos/{owner}/{repo}/history endpoint",
                     description="Add the history route", id="OPP-1")
        fresh = _opp("Add /api/build/diff endpoint",
                     description="Add a brand new diff route", id="OPP-2")
        kept = oc.filter_already_built([built, fresh], self._TREE, self._KEY)
        assert [o.id for o in kept] == ["OPP-2"]
        assert built.worth_doing is False
        assert "already built" in (built.verify_reason or "")


# ── PlannerAgent ─────────────────────────────────────────────────────────────

class TestPlanner:
    def test_fallback_single_step_without_provider(self):
        # No provider configured -> get_provider raises -> fallback plan.
        agent = PlannerAgent(provider=None)
        # Force provider resolution to fail.
        agent._get_provider = lambda: (_ for _ in ()).throw(RuntimeError("no provider"))
        plan = agent.plan(_opp("Cache the tree"), file_tree=["backend/app/main.py"])
        assert isinstance(plan, ExecutionPlan)
        assert len(plan.steps) == 1
        assert plan.steps[0].index == 1

    def test_expands_and_topo_orders(self):
        from app.agents.planner_agent import _PlannerOutput, _PlannedStep
        out = _PlannerOutput(
            summary="do it in two steps",
            steps=[
                _PlannedStep(title="Wire route", description="d", target_files=["r.py"], depends_on=[2], kind="milestone"),
                _PlannedStep(title="Add model", description="d", target_files=["m.py"], depends_on=[], kind="milestone"),
            ],
            notes=[],
        )
        agent = PlannerAgent(provider=_FakeProvider(planner_out=out))
        plan = agent.plan(_opp("Persist"), file_tree=["m.py", "r.py"])
        # 'Add model' (no deps) must come before 'Wire route' (depends on it).
        assert [s.title for s in plan.steps] == ["Add model", "Wire route"]
        assert plan.steps[0].index == 1 and plan.steps[1].index == 2
        # depends_on rewritten to the new 1-based ordering (step 2 -> depends 1).
        assert plan.steps[1].depends_on == [1]


# ── PlanCritic ───────────────────────────────────────────────────────────────

def _plan(steps):
    return ExecutionPlan(opportunity_id="OPP-001", opportunity_title="t",
                         summary="s", steps=steps)


class TestPlanCritic:
    def test_empty_plan_rejected_structurally(self):
        crit = pc.critique_plan(_plan([]), opportunity=_opp(), provider=None)
        assert crit.approved is False
        assert any("no steps" in i for i in crit.issues)

    def test_forward_dependency_rejected(self):
        steps = [
            BuildStep(index=1, title="a", description="d", target_files=["x.py"], depends_on=[2]),
            BuildStep(index=2, title="b", description="d", target_files=["y.py"], depends_on=[]),
        ]
        crit = pc.critique_plan(_plan(steps), opportunity=_opp(), provider=None)
        assert crit.approved is False

    def test_step_without_target_files_rejected(self):
        steps = [BuildStep(index=1, title="a", description="d", target_files=[])]
        crit = pc.critique_plan(_plan(steps), opportunity=_opp(), provider=None)
        assert crit.approved is False
        assert any("no target_files" in i for i in crit.issues)

    def test_clean_plan_approved_without_llm(self):
        steps = [
            BuildStep(index=1, title="a", description="d", target_files=["x.py"]),
            BuildStep(index=2, title="b", description="d", target_files=["y.py"], depends_on=[1]),
        ]
        crit = pc.critique_plan(_plan(steps), opportunity=_opp(), provider=None)
        assert crit.approved is True

    def test_llm_rejection_blocks_clean_structure(self):
        from app.services.plan_critic import _PlanVerdict
        steps = [BuildStep(index=1, title="a", description="d", target_files=["x.py"])]
        provider = _FakeProvider(plan_verdict=_PlanVerdict(
            coherent=True, complete=False, in_scope=True,
            issues=["route added but never registered"], reason="incomplete",
        ))
        crit = pc.critique_plan(_plan(steps), opportunity=_opp(), provider=provider)
        assert crit.approved is False
        assert crit.complete is False


# ── /api/build/execute route ─────────────────────────────────────────────────

class TestExecuteRoute:
    _BODY = {
        "owner": "o", "repo": "r", "branch": "main", "access_token": "t",
        "opportunity": {
            "id": "OPP-001", "title": "Cache the tree", "category": "improvement",
            "description": "d", "impact": "faster", "effort": "S", "estimated_days": 2,
            "target_files": ["backend/app/main.py"], "evidence": ["backend/app/main.py"],
            "suggested_approach": ["add cache"], "rationale": "r",
        },
        "execute": False,
    }

    def _patch_common(self, monkeypatch):
        import app.api.routes.build as build_mod
        from app.services.repo_analysis_service import RepoAnalysisService
        from app.agents.repo_lens_agent import RepoLensAgent

        async def ok_auth(token, owner, repo): return None
        monkeypatch.setattr(build_mod, "verify_repo_write_access", ok_auth)

        async def fake_ctx(*a, **k):
            return {"repo_info": {"owner": "o", "name": "r", "full_name": "o/r"},
                    "file_tree": ["backend/app/main.py"], "key_files": {}, "branch": "main"}
        monkeypatch.setattr(RepoAnalysisService, "build_context", classmethod(lambda cls, **k: fake_ctx(**k)))
        # Skip the real RepoLens (no key_files to analyze here).
        monkeypatch.setattr(RepoLensAgent, "run", lambda self, ctx: None)

    def test_plan_only_does_not_execute(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.agents.planner_agent import PlannerAgent

        self._patch_common(monkeypatch)
        plan = ExecutionPlan(
            opportunity_id="OPP-001", opportunity_title="Cache the tree", summary="s",
            steps=[BuildStep(index=1, title="add cache", description="d",
                             target_files=["backend/app/main.py"])],
        )
        monkeypatch.setattr(PlannerAgent, "plan", lambda self, opp, ft=None: plan)

        client = TestClient(app)
        resp = client.post("/api/build/execute", json=self._BODY)
        assert resp.status_code == 200
        data = resp.json()
        assert data["executed"] is False
        assert data["status"] == "planned"
        assert data["plan"]["steps"][0]["title"] == "add cache"

    def test_execute_blocked_when_critic_rejects(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        from app.agents.planner_agent import PlannerAgent
        from app.services import plan_critic as pcmod
        from app.schemas.agent_schemas import PlanCritique

        self._patch_common(monkeypatch)
        plan = ExecutionPlan(
            opportunity_id="OPP-001", opportunity_title="t", summary="s",
            steps=[BuildStep(index=1, title="s1", description="d", target_files=["x.py"])],
        )
        monkeypatch.setattr(PlannerAgent, "plan", lambda self, opp, ft=None: plan)
        monkeypatch.setattr(pcmod, "critique_plan",
                            lambda plan, opportunity=None, provider=None: PlanCritique(
                                approved=False, reason="not coherent"))

        # Guard: run_actuation must NEVER be called when the critic rejects.
        from app.services.coder_orchestrator import CoderOrchestrator
        called = {"n": 0}
        async def boom(cls, req, on_event=None):
            called["n"] += 1
            raise AssertionError("run_actuation should not run on a rejected plan")
        monkeypatch.setattr(CoderOrchestrator, "run_actuation", classmethod(boom))

        body = dict(self._BODY, execute=True)
        client = TestClient(app)
        resp = client.post("/api/build/execute", json=body)
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "plan_rejected"
        assert data["executed"] is False
        assert called["n"] == 0
