"""End-to-end smoke test for POST /api/analyze.

CI's docker-smoke only proves the container BOOTS (GET /health). It never
exercised an actual analysis, so the boto3-missing bug (Bedrock is the default
provider, imported lazily) sailed through every gate and would only surface on
the first real /analyze in production. This test drives the full analyze
pipeline — route → RepoIndex → 4-agent orchestrator → scoring → report assembly
→ persistence — with the GitHub fetch + write-access stubbed and NO LLM
(agents fail-open to heuristics), so it runs offline in CI and asserts the
pipeline produces a valid report end to end.
"""
import os

import pytest

from app.services import sqlite_store


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    monkeypatch.setenv("SHIPMATE_STORE_DIR", str(tmp_path))
    monkeypatch.delenv("SHIPMATE_INFLIGHT_DB", raising=False)
    monkeypatch.delenv("SHIPMATE_REPORTS_DB", raising=False)
    sqlite_store.close_all()
    from app.services.repo_index_service import RepoIndexService
    RepoIndexService.clear_cache()
    # Force the NO-LLM path: every agent fail-opens to heuristics, so the test
    # is hermetic + fast (no Bedrock/Azure network call). This is exactly the
    # degraded path a clean deploy without creds would take — the one we want
    # CI to verify still produces a valid report.
    import app.services.llm_service as llm
    monkeypatch.setattr(llm, "_get_provider", lambda: None)
    yield
    sqlite_store.close_all()


def _fake_repo_index():
    """A minimal but realistic RepoIndex the orchestrator can score."""
    from app.services.repo_index_service import RepoIndex
    repo_context = {
        "repo_info": {
            "owner": {"login": "acme"}, "name": "widget",
            "full_name": "acme/widget", "language": "Python",
            "stargazers_count": 7, "html_url": "https://github.com/acme/widget",
        },
        "file_tree": ["app/main.py", "tests/test_x.py", "requirements.txt", "README.md"],
        "key_files": {
            "app/main.py": "from fastapi import FastAPI\napp = FastAPI()\n",
            "requirements.txt": "fastapi\n",
            "README.md": "# widget\n",
        },
        "branch": "main",
        "pr_info": None,
        "warnings": [],
    }
    return RepoIndex(repo_context=repo_context, repo_lens=None,
                     built_at=0.0, source_enriched=True)


def test_analyze_happy_path_returns_report(monkeypatch):
    from fastapi.testclient import TestClient
    from app.main import app
    import app.api.routes.analysis as analysis_mod
    from app.services.repo_index_service import RepoIndexService

    # Stub auth (write-access OK) and the GitHub-backed context build. The REAL
    # orchestrator then runs — with no LLM provider it fail-opens to heuristic
    # outputs, which is exactly the offline path we want to verify in CI.
    async def ok_auth(token, owner, repo):
        return None
    monkeypatch.setattr(analysis_mod, "verify_repo_write_access", ok_auth)

    async def fake_index(cls, **kwargs):
        return _fake_repo_index()
    monkeypatch.setattr(RepoIndexService, "get_or_build", classmethod(fake_index))

    client = TestClient(app)
    resp = client.post("/api/analyze", json={
        "owner": "acme", "repo": "widget", "branch": "main", "access_token": "t",
    })

    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["status"] == "complete"
    report = body["report"]
    # The pipeline produced a real, well-formed report.
    assert isinstance(report["readiness_score"], int)
    assert 0 <= report["readiness_score"] <= 100
    assert report["ship_recommendation"]
    assert report["repo"]["full_name"] == "acme/widget"
    # All four agents are present in the assembled output.
    for agent in ("repo_lens", "plan_forge", "guardrail", "testpilot"):
        assert agent in report["agents"], f"missing agent: {agent}"


def test_analyze_persists_to_history(monkeypatch):
    """A successful analyze writes a row the history endpoint can read back —
    proves the report_store persistence path is wired through the route."""
    from fastapi.testclient import TestClient
    from app.main import app
    import app.api.routes.analysis as analysis_mod
    from app.services.repo_index_service import RepoIndexService

    async def ok_auth(token, owner, repo):
        return None
    monkeypatch.setattr(analysis_mod, "verify_repo_write_access", ok_auth)

    async def fake_index(cls, **kwargs):
        return _fake_repo_index()
    monkeypatch.setattr(RepoIndexService, "get_or_build", classmethod(fake_index))

    client = TestClient(app)
    r1 = client.post("/api/analyze", json={
        "owner": "acme", "repo": "widget", "branch": "main", "access_token": "t",
    })
    assert r1.status_code == 200

    hist = client.get("/api/repos/acme/widget/history")
    assert hist.status_code == 200
    runs = hist.json()["runs"]
    assert len(runs) >= 1
    assert runs[0]["readiness_score"] == r1.json()["report"]["readiness_score"]
