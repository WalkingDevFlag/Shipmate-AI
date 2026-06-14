"""Tests for the research harness SSE endpoint (routes/research.py).

Mocks the heavy collaborators — credential resolution, write-access check,
RepoIndexService, ResearchService, and the opportunity engines — so the harness
streams deterministically with no network/Bedrock/GitHub. Assertions are on the
SSE event sequence the Research UI consumes, and on the mode routing
(research / improve / innovate).
"""
import json
import os
import tempfile
from unittest.mock import AsyncMock, patch

import pytest
from fastapi.testclient import TestClient

_TMP_DB = os.path.join(tempfile.gettempdir(), "shipmate_research_test.db")
os.environ["SHIPMATE_INFLIGHT_DB"] = _TMP_DB

from app.main import app  # noqa: E402
from app.schemas.agent_schemas import (  # noqa: E402
    BuildPlanResponse, Opportunity, ResearchFinding, ResearchReport,
)

client = TestClient(app)


def _parse_sse(text: str):
    return [json.loads(ln[len("data: "):]) for ln in text.splitlines() if ln.startswith("data: ")]


def _report(findings=None):
    return ResearchReport(
        owner="o", repo="r", branch="main", question="q",
        answer="Here is the answer.",
        findings=findings if findings is not None else [
            ResearchFinding(title="Break a<->b cycle", kind="coupling", severity="high",
                            detail="circular import", evidence=["backend/app/a.py"],
                            suggested_action="extract helper", graph_signal="cycle: a.py<->b.py"),
        ],
        graph_summary={"module_count": 12, "cycles": [["a", "b"]],
                       "god_modules": [{"path": "x", "fan_in": 9}], "orphans": ["z"]},
        ai_enhanced=True,
    )


def _plan(opps=None):
    return BuildPlanResponse(
        owner="o", repo="r", branch="main",
        opportunities=opps if opps is not None else [
            Opportunity(id="OPP-001", title="Cache the tree", category="improvement",
                        description="d", impact="faster", effort="M", estimated_days=3,
                        value_score=72, priority="high", target_files=["backend/app/x.py"]),
        ],
        total_found=1, grounded_count=1, ai_enhanced=True, generated_at="now",
    )


class _FakeIndex:
    repo_context = {"key_files": {"backend/app/a.py": "x"}, "repo_info": {"name": "r"}}
    repo_lens = None


def _common_patches():
    return [
        patch("app.api.deps.resolve_credential", return_value="tok"),
        patch("app.api.deps.verify_repo_write_access", new=AsyncMock(return_value=None)),
        patch("app.services.repo_index_service.RepoIndexService.get_or_build",
              new=AsyncMock(return_value=_FakeIndex())),
        patch("app.services.research_service.ResearchService.research",
              return_value=_report()),
    ]


def _run(body, extra=()):
    patches = _common_patches() + list(extra)
    started = [p.start() for p in patches]
    try:
        with client.stream("POST", "/api/research", json=body) as resp:
            assert resp.status_code == 200
            assert resp.headers["content-type"].startswith("text/event-stream")
            return _parse_sse(resp.read().decode())
    finally:
        for p in patches:
            p.stop()


BODY = {"owner": "o", "repo": "r", "access_token": "sess", "question": "any cycles?"}


class TestResearchMode:
    def test_research_mode_streams_graph_and_findings(self):
        events = _run({**BODY, "mode": "research"})
        kinds = [e["event"] for e in events]
        assert "research.start" in kinds
        assert "index.done" in kinds
        assert "graph.done" in kinds
        assert "finding" in kinds
        assert "research.done" in kinds
        assert kinds[-1] == "done"
        # graph.done carries the computed signals
        gd = next(e for e in events if e["event"] == "graph.done")
        assert gd["modules"] == 12 and gd["cycles"] == 1 and gd["god_modules"] == 1
        # finding carries the graph_signal grounding
        f = next(e for e in events if e["event"] == "finding")
        assert f["graph_signal"] == "cycle: a.py<->b.py"
        # research mode does NOT propose
        assert "propose.start" not in kinds
        assert next(e for e in events if e["event"] == "done")["opportunities"] == 0

    def test_research_mode_does_not_call_opportunity_engine(self):
        with patch("app.services.opportunity_service.OpportunityService.build_plan") as bp:
            _run({**BODY, "mode": "research"})
            bp.assert_not_called()


class TestImproveMode:
    def test_improve_mode_proposes_via_opportunity_engine(self):
        with patch("app.services.opportunity_service.OpportunityService.build_plan",
                   return_value=_plan()) as bp, \
             patch("app.services.opportunity_service.OpportunityService.build_innovation_plan") as bip:
            events = _run({**BODY, "mode": "improve"})
            bp.assert_called_once()
            bip.assert_not_called()
        kinds = [e["event"] for e in events]
        assert "propose.start" in kinds and "opportunity" in kinds and "propose.done" in kinds
        opp = next(e for e in events if e["event"] == "opportunity")
        assert opp["title"] == "Cache the tree" and opp["value_score"] == 72
        assert next(e for e in events if e["event"] == "done")["opportunities"] == 1


class TestInnovateMode:
    def test_innovate_mode_routes_to_innovation_engine(self):
        with patch("app.services.opportunity_service.OpportunityService.build_innovation_plan",
                   return_value=_plan()) as bip, \
             patch("app.services.opportunity_service.OpportunityService.build_plan") as bp:
            events = _run({**BODY, "mode": "innovate"})
            bip.assert_called_once()
            bp.assert_not_called()
        assert any(e["event"] == "opportunity" for e in events)


class TestFailOpen:
    def test_bad_credential_ends_with_error(self):
        with patch("app.api.deps.resolve_credential", return_value=None):
            with client.stream("POST", "/api/research", json=BODY) as resp:
                events = _parse_sse(resp.read().decode())
        assert events and events[-1]["event"] == "error"

    def test_missing_fields_error_frame(self):
        with client.stream("POST", "/api/research", json={"owner": "", "repo": "", "access_token": ""}) as resp:
            events = _parse_sse(resp.read().decode())
        assert events[-1]["event"] == "error"

    def test_propose_failure_still_delivers_findings(self):
        with patch("app.services.opportunity_service.OpportunityService.build_plan",
                   side_effect=RuntimeError("bedrock down")):
            events = _run({**BODY, "mode": "improve"})
        kinds = [e["event"] for e in events]
        # research findings still delivered; an error frame is emitted; stream ends with done.
        assert "finding" in kinds
        assert any(e["event"] == "error" for e in events)
        assert kinds[-1] == "done"
