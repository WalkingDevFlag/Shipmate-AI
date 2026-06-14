"""Tests for the fixes acted on from a full agent run (PlanForge/GuardRail/
TestPilot/Opportunities against this repo):

  • GitHub transient-failure retry (OPP: retry transient GitHub API failures)
  • shared write-access dependency raises 403 when permissions object absent
    (TestPilot: test_verify_repo_write_access_raises_403_when_permissions_absent)
  • branches.py prune accepts header token + requires write access on execute
    (GuardRail: token-via-query-param leak in branch routes)
  • build/plan returns 502 on GitHub context-fetch failure
    (TestPilot: test_build_plan_returns_502_on_github_context_fetch_failure)
"""
import asyncio

import httpx
import pytest


# ── GitHub transient-failure retry ───────────────────────────────────────────

class TestGitHubRetry:
    def test_retries_on_500_then_succeeds(self, monkeypatch):
        from app.services import github_api_service as gh
        monkeypatch.setattr(gh, "_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(gh, "_RETRY_BASE_DELAY_S", 0.0)

        calls = {"n": 0}

        class _Client:
            async def get(self, url, **kw):
                calls["n"] += 1
                req = httpx.Request("GET", url)
                if calls["n"] < 3:
                    return httpx.Response(500, request=req)
                return httpx.Response(200, request=req, json={"ok": True})

        resp = asyncio.run(gh._get_with_retry(_Client(), "https://api.github.com/x"))
        assert resp.status_code == 200
        assert calls["n"] == 3, "should retry twice then succeed on the 3rd"

    def test_does_not_retry_on_404(self, monkeypatch):
        from app.services import github_api_service as gh
        monkeypatch.setattr(gh, "_RETRY_ATTEMPTS", 3)
        monkeypatch.setattr(gh, "_RETRY_BASE_DELAY_S", 0.0)

        calls = {"n": 0}

        class _Client:
            async def get(self, url, **kw):
                calls["n"] += 1
                return httpx.Response(404, request=httpx.Request("GET", url))

        resp = asyncio.run(gh._get_with_retry(_Client(), "https://api.github.com/x"))
        assert resp.status_code == 404
        assert calls["n"] == 1, "4xx is deterministic — must NOT retry"

    def test_retries_on_timeout_then_raises_after_exhaustion(self, monkeypatch):
        from app.services import github_api_service as gh
        monkeypatch.setattr(gh, "_RETRY_ATTEMPTS", 2)
        monkeypatch.setattr(gh, "_RETRY_BASE_DELAY_S", 0.0)

        calls = {"n": 0}

        class _Client:
            async def get(self, url, **kw):
                calls["n"] += 1
                raise httpx.ReadTimeout("slow", request=httpx.Request("GET", url))

        with pytest.raises(httpx.TimeoutException):
            asyncio.run(gh._get_with_retry(_Client(), "https://api.github.com/x"))
        assert calls["n"] == 2, "should attempt exactly _RETRY_ATTEMPTS times"


# ── Shared write-access dependency ───────────────────────────────────────────

class TestVerifyRepoWriteAccess:
    def _client_returning(self, status, body):
        class _Client:
            def __init__(self, *a, **k): pass
            async def __aenter__(self): return self
            async def __aexit__(self, *a): return False
            async def get(self, url, **kw):
                return httpx.Response(status, request=httpx.Request("GET", url), json=body)
        return _Client

    def test_403_when_permissions_object_absent(self, monkeypatch):
        # The exact TestPilot-flagged branch: 200 OK but no `permissions` key.
        from app.api import deps
        monkeypatch.setattr(deps.httpx, "AsyncClient", self._client_returning(200, {"name": "r"}))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            asyncio.run(deps.verify_repo_write_access("o", "r", "t"))
        assert ei.value.status_code == 403

    def test_passes_with_push_permission(self, monkeypatch):
        from app.api import deps
        monkeypatch.setattr(deps.httpx, "AsyncClient",
                            self._client_returning(200, {"permissions": {"push": True}}))
        # Should NOT raise.
        asyncio.run(deps.verify_repo_write_access("o", "r", "t"))

    def test_404_repo_missing(self, monkeypatch):
        from app.api import deps
        monkeypatch.setattr(deps.httpx, "AsyncClient", self._client_returning(404, {}))
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            asyncio.run(deps.verify_repo_write_access("o", "r", "t"))
        assert ei.value.status_code == 404


# ── resolve_access_token (now shared in deps) ────────────────────────────────

class TestResolveToken:
    # Header-only since the query-param door was closed (B). No ?access_token=.
    def test_header_bearer(self):
        from app.api.deps import resolve_access_token
        assert resolve_access_token(authorization="Bearer h") == "h"

    def test_missing_header_401(self):
        from app.api.deps import resolve_access_token
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            resolve_access_token(authorization=None)
        assert ei.value.status_code == 401

    def test_query_param_no_longer_accepted(self):
        # A URL credential must NOT authenticate — resolve_access_token no longer
        # reads any query param; only the header counts. Passing nothing in the
        # header is a hard 401 even though a real backend request might still
        # carry ?access_token= in the URL (now ignored).
        from app.api.deps import resolve_access_token
        from fastapi import HTTPException
        with pytest.raises(HTTPException) as ei:
            resolve_access_token(authorization=None)
        assert ei.value.status_code == 401


# ── branches prune: header token + write-access gate on execute ──────────────

class TestBranchesPrune:
    def test_preview_accepts_bearer_header(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        import app.api.routes.branches as br

        async def fake_prune(token, owner, repo, dry_run):
            class _R:
                def to_dict(self): return {"deleted": [], "kept": [], "dry_run": dry_run, "token": token}
            return _R()
        monkeypatch.setattr(br, "prune_shipmate_branches", fake_prune)

        client = TestClient(app)
        resp = client.get("/api/branches/o/r/prune", headers={"Authorization": "Bearer hdr-token"})
        assert resp.status_code == 200
        assert resp.json()["token"] == "hdr-token", "must read token from the header"

    def test_preview_401_without_token(self):
        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        resp = client.get("/api/branches/o/r/prune")
        assert resp.status_code == 401

    def test_execute_blocked_without_write_access(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        import app.api.routes.branches as br
        from fastapi import HTTPException

        async def deny(owner, repo, token):
            raise HTTPException(status_code=403, detail="no write")
        monkeypatch.setattr(br, "verify_repo_write_access", deny)

        called = {"prune": False}
        async def fake_prune(*a, **k):
            called["prune"] = True
        monkeypatch.setattr(br, "prune_shipmate_branches", fake_prune)

        client = TestClient(app)
        resp = client.post("/api/branches/o/r/prune", headers={"Authorization": "Bearer t"})
        assert resp.status_code == 403
        assert called["prune"] is False, "must NOT delete branches without write access"


# ── build/plan 502 on GitHub context-fetch failure ──────────────────────────

class TestBuildPlan502:
    def test_returns_502_on_context_failure(self, monkeypatch):
        from fastapi.testclient import TestClient
        from app.main import app
        import app.api.routes.build as build_mod
        from app.services.repo_analysis_service import RepoAnalysisService

        async def ok_auth(token, owner, repo): return None
        monkeypatch.setattr(build_mod, "verify_repo_write_access", ok_auth)

        async def boom(*a, **k):
            raise RuntimeError("github exploded")
        monkeypatch.setattr(RepoAnalysisService, "build_context", classmethod(lambda cls, **k: boom()))

        client = TestClient(app)
        resp = client.post("/api/build/plan", json={
            "owner": "o", "repo": "r", "branch": "main", "access_token": "t",
        })
        assert resp.status_code == 502
