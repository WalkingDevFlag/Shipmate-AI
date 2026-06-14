"""Consolidated input-sanitization middleware tests for ShipMate AI backend.

Covers:
- Async (anyio) tests via httpx.AsyncClient + ASGITransport against /health and
  /api/analyze to verify the middleware stack end-to-end without requiring a
  running server.
- Sync TestClient tests against real endpoints (/api/analysis/analyze) for JSON,
  form-encoded, multipart, and query-parameter payloads.
- Blocked patterns: eval(, exec(, __import__, subprocess, os.system, __globals__.
- Nested/list/case-insensitive detection.
- Allowed (clean) payloads must not be blocked by the middleware (downstream
  may return 401/422 but never 400 from the sanitizer).
- Exempt headers: Authorization and Cookie must not be scanned.
"""
import io

import pytest
from httpx import AsyncClient, ASGITransport
from fastapi.testclient import TestClient

from app.main import app


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

BLOCKED_DETAIL = "Request contains disallowed content"

sync_client = TestClient(app, raise_server_exceptions=False)


def _assert_blocked(response):
    assert response.status_code == 400, (
        f"Expected 400 but got {response.status_code}; body={response.text}"
    )
    assert BLOCKED_DETAIL in response.text


# ---------------------------------------------------------------------------
# Async (anyio) tests — middleware stack via httpx.AsyncClient
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_clean_get_request_passes():
    """A normal GET request with no dangerous content must reach the handler."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health")
    assert response.status_code == 200


@pytest.mark.anyio
async def test_dangerous_query_param_blocked_async():
    """A query parameter containing eval( must be rejected with 400."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.get("/health", params={"q": "eval(malicious()"})
    assert response.status_code == 400


@pytest.mark.anyio
async def test_dangerous_body_eval_blocked_async():
    """A JSON body containing eval( must be rejected with 400."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/analyze",
            json={"repo_url": "eval(os.system('id'))"},
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_dangerous_body_exec_blocked_async():
    """A JSON body containing exec( must be rejected with 400."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/analyze",
            json={"prompt": "exec(open('/etc/passwd').read())"},
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_dangerous_body_import_blocked_async():
    """A JSON body containing __import__ must be rejected with 400."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/analyze",
            json={"repo_url": "__import__('os').system('id')"},
            headers={"content-type": "application/json"},
        )
    assert response.status_code == 400


@pytest.mark.anyio
async def test_clean_post_body_not_blocked_async():
    """A POST body with a legitimate repo URL must NOT be blocked by the middleware."""
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as client:
        response = await client.post(
            "/api/analyze",
            json={"repo_url": "https://github.com/octocat/Hello-World", "branch": "main"},
            headers={"content-type": "application/json"},
        )
    # Middleware must NOT block this — downstream may return 401/422 but not 400 from sanitizer
    assert response.status_code != 400


# ---------------------------------------------------------------------------
# JSON body tests (sync)
# ---------------------------------------------------------------------------

class TestJsonBodyBlocked:
    """Dangerous patterns in JSON bodies must be blocked with 400."""

    def test_eval_in_top_level_string_value(self):
        payload = {"prompt": "eval(malicious_code())"}
        _assert_blocked(sync_client.post("/api/analysis/analyze", json=payload))

    def test_exec_in_nested_dict_value(self):
        payload = {"outer": {"inner": "exec(open('/etc/passwd').read())"}}
        _assert_blocked(sync_client.post("/api/analysis/analyze", json=payload))

    def test_dunder_import_in_list_element(self):
        payload = {"items": ["safe", "__import__('os').system('id')"]}
        _assert_blocked(sync_client.post("/api/analysis/analyze", json=payload))

    def test_subprocess_in_json_value(self):
        payload = {"cmd": "subprocess.run(['ls', '-la'])"}
        _assert_blocked(sync_client.post("/api/analysis/analyze", json=payload))

    def test_os_system_in_json_value(self):
        payload = {"action": "os.system('rm -rf /')"}
        _assert_blocked(sync_client.post("/api/analysis/analyze", json=payload))

    def test_globals_dunder_in_json_value(self):
        payload = {"x": "print(__globals__)"}
        _assert_blocked(sync_client.post("/api/analysis/analyze", json=payload))

    def test_dangerous_pattern_case_insensitive(self):
        """Dangerous patterns should be detected case-insensitively (e.g. EVAL()."""
        payload = {"code": "EVAL(something)"}
        response = sync_client.post("/api/analysis/analyze", json=payload)
        assert response.status_code == 400
        assert BLOCKED_DETAIL in response.text


class TestJsonBodyAllowed:
    """Clean JSON bodies must not be blocked by the middleware."""

    def test_clean_json_not_blocked(self):
        payload = {"repo": "owner/repo", "branch": "main"}
        resp = sync_client.post("/api/analysis/analyze", json=payload)
        assert resp.status_code != 400, (
            f"Clean payload was incorrectly blocked: {resp.text}"
        )

    def test_clean_analysis_prompt_not_blocked(self):
        payload = {"prompt": "Please analyze this code for vulnerabilities"}
        resp = sync_client.post("/api/analysis/analyze", json=payload)
        assert resp.status_code != 400, (
            f"Clean prompt was incorrectly blocked: {resp.text}"
        )

    def test_empty_body_not_blocked(self):
        """POST with empty JSON object must not be blocked by sanitization."""
        resp = sync_client.post("/api/analysis/analyze", json={})
        assert resp.status_code != 400, (
            f"Empty body was incorrectly blocked: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Form-encoded body tests
# ---------------------------------------------------------------------------

class TestFormBodyBlocked:
    """Dangerous patterns in application/x-www-form-urlencoded bodies must be blocked."""

    def test_eval_in_form_field(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            data={"field": "eval(dangerous())"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _assert_blocked(resp)

    def test_exec_in_form_field(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            data={"cmd": "exec('import os')"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _assert_blocked(resp)

    def test_subprocess_in_form_field(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            data={"value": "subprocess.Popen(['id'])"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        _assert_blocked(resp)


class TestFormBodyAllowed:
    """Clean form bodies must not be blocked by the middleware."""

    def test_clean_form_not_blocked(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            data={"repo": "owner/repo", "branch": "main"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert resp.status_code != 400, (
            f"Clean form payload was incorrectly blocked: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Multipart body tests
# ---------------------------------------------------------------------------

class TestMultipartBodyBlocked:
    """Dangerous patterns in multipart/form-data bodies must be blocked."""

    def test_eval_in_multipart_field(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            files={"upload": ("test.txt", io.BytesIO(b"eval(bad())"), "text/plain")},
        )
        _assert_blocked(resp)

    def test_exec_in_multipart_field(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            files={"upload": ("test.txt", io.BytesIO(b"exec('import subprocess')"), "text/plain")},
        )
        _assert_blocked(resp)

    def test_dunder_import_in_multipart_field(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            files={"upload": ("test.txt", io.BytesIO(b"__import__('os')"), "text/plain")},
        )
        _assert_blocked(resp)


class TestMultipartBodyAllowed:
    """Clean multipart bodies must not be blocked by the middleware."""

    def test_clean_multipart_not_blocked(self):
        resp = sync_client.post(
            "/api/analysis/analyze",
            files={"upload": ("readme.txt", io.BytesIO(b"Hello, world!"), "text/plain")},
        )
        assert resp.status_code != 400, (
            f"Clean multipart payload was incorrectly blocked: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Query parameter tests
# ---------------------------------------------------------------------------

class TestQueryParamBlocked:
    """Dangerous patterns in query parameters must be blocked."""

    def test_eval_in_query_param(self):
        _assert_blocked(sync_client.get("/api/analysis/analyze?q=eval(bad())"))

    def test_subprocess_in_query_param(self):
        _assert_blocked(sync_client.get("/api/analysis/analyze?cmd=subprocess.run(['id'])"))


class TestQueryParamAllowed:
    """Clean query parameters must not be blocked by the middleware."""

    def test_clean_query_not_blocked(self):
        resp = sync_client.get("/api/analysis/analyze?repo=owner%2Frepo&branch=main")
        assert resp.status_code != 400, (
            f"Clean query param was incorrectly blocked: {resp.text}"
        )


# ---------------------------------------------------------------------------
# Exempt headers: Authorization and Cookie must not be scanned
# ---------------------------------------------------------------------------

class TestExemptHeaders:
    """Authorization and Cookie headers must not be scanned for dangerous patterns."""

    def test_authorization_header_with_eval_not_blocked(self):
        """
        Bearer tokens that incidentally contain 'eval(' must not be rejected.
        Confirms the intentional exclusion of 'authorization' from header scanning.
        """
        bearer_token = "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eval(test).signature"
        response = sync_client.get(
            "/health",
            headers={"Authorization": bearer_token},
        )
        assert response.status_code == 200
        assert response.json() == {"status": "healthy", "agents": 4}

    def test_cookie_header_with_eval_not_blocked(self):
        """Cookie header containing dangerous patterns must not be rejected by sanitization."""
        response = sync_client.get(
            "/health",
            headers={"Cookie": "session=eval(x)"},
        )
        assert response.status_code != 400
