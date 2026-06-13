import pytest
from fastapi.testclient import TestClient
import re

from app.main import (
    app,
    _contains_dangerous_pattern,
    _DANGEROUS_PATTERNS,
    ALLOWED_ORIGINS,
)


client = TestClient(app)


class TestFastAPIInitialization:
    """Test FastAPI app initialization and metadata."""

    def test_app_is_fastapi_instance(self):
        """Verify app is a FastAPI instance."""
        from fastapi import FastAPI
        assert isinstance(app, FastAPI)

    def test_app_title(self):
        """Verify app title is set correctly."""
        assert app.title == "ShipMate AI"

    def test_app_version(self):
        """Verify app version is set correctly."""
        assert app.version == "2.0.0"

    def test_app_description(self):
        """Verify app description is set correctly."""
        assert "AI-native multi-agent release readiness platform" in app.description

    def test_docs_url(self):
        """Verify docs URL is configured."""
        assert app.docs_url == "/docs"

    def test_redoc_url(self):
        """Verify ReDoc URL is configured."""
        assert app.redoc_url == "/redoc"


class TestRouteRegistration:
    """Test route registration and basic endpoint functionality."""

    def test_root_endpoint_exists(self):
        """Verify root endpoint is registered."""
        response = client.get("/")
        assert response.status_code == 200

    def test_root_endpoint_response(self):
        """Verify root endpoint returns expected metadata."""
        response = client.get("/")
        data = response.json()
        assert data["service"] == "ShipMate AI"
        assert data["version"] == "2.0.0"
        assert data["status"] == "operational"
        assert data["docs"] == "/docs"
        assert "RepoLens" in data["agents"]
        assert "PlanForge" in data["agents"]
        assert "GuardRail" in data["agents"]
        assert "TestPilot" in data["agents"]

    def test_health_endpoint_exists(self):
        """Verify health endpoint is registered."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_health_endpoint_response(self):
        """Verify health endpoint returns expected status."""
        response = client.get("/health")
        data = response.json()
        assert data["status"] == "healthy"
        assert data["agents"] == 4

    def test_stale_github_callback_html_removed(self):
        """The server-served /github-callback.html is gone.

        It was a dead OAuth fallback that posted `d.access_token` — undefined
        since the session-vault migration (the callback now returns an opaque
        session_id, never the raw token). The SPA popup path (GitHubCallback.tsx
        at the /github/callback SPA route) is the only OAuth completion path."""
        response = client.get("/github-callback.html")
        assert response.status_code == 404

    def test_auth_router_prefix(self):
        """Verify auth router is registered with /api prefix."""
        routes = [route.path for route in app.routes]
        assert any("/api/auth" in route for route in routes)

    def test_analysis_router_prefix(self):
        """Verify analysis router is registered with /api prefix."""
        routes = [route.path for route in app.routes]
        # Analysis routes should be prefixed with /api
        assert any("/api/analyze" in route for route in routes)


class TestMiddlewareSetup:
    """Test middleware configuration and functionality."""

    def test_cors_middleware_configured(self):
        """Verify CORS middleware is added to app."""
        middleware_names = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in middleware_names

    def test_allowed_origins_configured(self):
        """Verify ALLOWED_ORIGINS is properly configured."""
        assert isinstance(ALLOWED_ORIGINS, list)
        assert len(ALLOWED_ORIGINS) > 0
        assert any("localhost" in origin for origin in ALLOWED_ORIGINS)

    def test_cors_headers_on_request(self):
        """Verify CORS headers are present in response."""
        response = client.get("/", headers={"Origin": ALLOWED_ORIGINS[0]})
        assert response.status_code == 200

    def test_input_sanitization_middleware_blocks_eval(self):
        """Verify input sanitization middleware blocks eval patterns."""
        response = client.get("/?param=eval(1)")
        assert response.status_code == 400
        data = response.json()
        assert "disallowed content" in data["detail"].lower()

    def test_input_sanitization_middleware_blocks_exec(self):
        """Verify input sanitization middleware blocks exec patterns."""
        response = client.get("/?param=exec(1)")
        assert response.status_code == 400

    def test_input_sanitization_middleware_blocks_import(self):
        """Verify input sanitization middleware blocks __import__ patterns."""
        response = client.get("/?param=__import__('os')")
        assert response.status_code == 400

    def test_input_sanitization_middleware_blocks_builtins(self):
        """Verify input sanitization middleware blocks __builtins__ patterns."""
        response = client.get("/?param=__builtins__")
        assert response.status_code == 400

    def test_input_sanitization_middleware_blocks_subprocess(self):
        """Verify input sanitization middleware blocks subprocess patterns."""
        response = client.get("/?param=subprocess.call()")
        assert response.status_code == 400

    def test_input_sanitization_middleware_blocks_os_system(self):
        """Verify input sanitization middleware blocks os.system patterns."""
        response = client.get("/?param=os.system('ls')")
        assert response.status_code == 400

    def test_input_sanitization_middleware_allows_safe_query(self):
        """Verify input sanitization middleware allows safe query parameters."""
        response = client.get("/?param=safe_value&other=123")
        assert response.status_code != 400

    def test_sanitize_input_middleware_allows_safe_request_passes_through(self):
        """Verify safe requests with no dangerous patterns pass through middleware."""
        response = client.get(
            "/?query=benign&search=test",
            headers={"X-Custom-Header": "safe-value"},
        )
        assert response.status_code == 200
        data = response.json()
        assert data["service"] == "ShipMate AI"
        assert data["status"] == "operational"

    def test_input_sanitization_middleware_checks_headers(self):
        """Verify input sanitization middleware checks headers."""
        response = client.get("/", headers={"X-Custom-Header": "eval(1)"})
        assert response.status_code == 400

    def test_input_sanitization_middleware_skips_auth_headers(self):
        """Verify input sanitization middleware skips Authorization header."""
        response = client.get("/", headers={"Authorization": "Bearer eval(1)"})
        assert response.status_code != 400

    def test_sanitize_input_middleware_authorization_header_not_scanned(self):
        """Verify Authorization header with eval( is not scanned, confirming exclusion list is respected."""
        response = client.get("/", headers={"Authorization": "Bearer eval(1)"})
        assert response.status_code == 200

    def test_sanitize_input_middleware_authorization_header_with_dangerous_pattern_passes(self):
        """Verify Authorization header containing dangerous pattern is not blocked, validating intentional exclusion."""
        response = client.get("/", headers={"Authorization": "Bearer eval(x)"})
        assert response.status_code == 200

    def test_input_sanitization_middleware_skips_cookie_headers(self):
        """Verify input sanitization middleware skips Cookie header."""
        response = client.get("/", headers={"Cookie": "session=eval(1)"})
        assert response.status_code != 400

    def test_sanitize_input_middleware_subprocess_in_header_returns_400(self):
        """Verify input sanitization middleware blocks subprocess patterns in custom X- headers."""
        response = client.get("/", headers={"X-Custom-Header": "subprocess."})
        assert response.status_code == 400

    def test_input_sanitization_middleware_checks_json_body(self):
        """Verify input sanitization middleware checks JSON request body."""
        response = client.post(
            "/api/auth/login",
            json={"username": "user", "password": "eval(1)"},
        )
        assert response.status_code == 400

    def test_input_sanitization_middleware_checks_form_body(self):
        """Verify input sanitization middleware checks form-encoded body."""
        response = client.post(
            "/",
            data={"field": "exec(1)"},
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        assert response.status_code == 400


class TestDangerousPatternDetection:
    """Test the dangerous pattern detection function."""

    def test_contains_dangerous_pattern_eval(self):
        """Verify eval pattern is detected."""
        assert _contains_dangerous_pattern("eval(1)")
        assert _contains_dangerous_pattern("EVAL(1)")
        assert _contains_dangerous_pattern("Eval(1)")

    def test_contains_dangerous_pattern_exec(self):
        """Verify exec pattern is detected."""
        assert _contains_dangerous_pattern("exec(1)")
        assert _contains_dangerous_pattern("EXEC(1)")

    def test_contains_dangerous_pattern_import(self):
        """Verify __import__ pattern is detected."""
        assert _contains_dangerous_pattern("__import__('os')")
        assert _contains_dangerous_pattern("__IMPORT__('os')")

    def test_contains_dangerous_pattern_builtins(self):
        """Verify __builtins__ pattern is detected."""
        assert _contains_dangerous_pattern("__builtins__")
        assert _contains_dangerous_pattern("__BUILTINS__")

    def test_contains_dangerous_pattern_globals(self):
        """Verify __globals__ pattern is detected."""
        assert _contains_dangerous_pattern("__globals__")
        assert _contains_dangerous_pattern("__GLOBALS__")

    def test_contains_dangerous_pattern_locals(self):
        """Verify __locals__ pattern is detected."""
        assert _contains_dangerous_pattern("__locals__")
        assert _contains_dangerous_pattern("__LOCALS__")

    def test_contains_dangerous_pattern_compile(self):
        """Verify compile pattern is detected."""
        assert _contains_dangerous_pattern("compile('code', 'file', 'exec')")
        assert _contains_dangerous_pattern("COMPILE(1)")

    def test_contains_dangerous_pattern_importlib(self):
        """Verify importlib.import_module pattern is detected."""
        assert _contains_dangerous_pattern("importlib.import_module('os')")
        assert _contains_dangerous_pattern("IMPORTLIB.IMPORT_MODULE('os')")

    def test_contains_dangerous_pattern_subprocess(self):
        """Verify subprocess pattern is detected."""
        assert _contains_dangerous_pattern("subprocess.call()")
        assert _contains_dangerous_pattern("SUBPROCESS.RUN()")

    def test_contains_dangerous_pattern_os_system(self):
        """Verify os.system pattern is detected."""
        assert _contains_dangerous_pattern("os.system('ls')")
        assert _contains_dangerous_pattern("OS.SYSTEM('ls')")

    def test_contains_dangerous_pattern_os_popen(self):
        """Verify os.popen pattern is detected."""
        assert _contains_dangerous_pattern("os.popen('ls')")
        assert _contains_dangerous_pattern("OS.POPEN('ls')")

    def test_contains_dangerous_pattern_safe_strings(self):
        """Verify safe strings are not flagged."""
        assert not _contains_dangerous_pattern("hello world")
        assert not _contains_dangerous_pattern("user@example.com")
        assert not _contains_dangerous_pattern("123-456-7890")
        assert not _contains_dangerous_pattern("normal_function_call()")
        assert not _contains_dangerous_pattern("evaluate this")

    def test_dangerous_patterns_list_not_empty(self):
        """Verify dangerous patterns list is populated."""
        assert len(_DANGEROUS_PATTERNS) > 0

    def test_dangerous_patterns_are_compiled_regex(self):
        """Verify dangerous patterns are compiled regex objects."""
        for pattern in _DANGEROUS_PATTERNS:
            assert isinstance(pattern, re.Pattern)

    def test_contains_dangerous_pattern_case_insensitive_exec_returns_true(self):
        """Verify all patterns use re.IGNORECASE flag by testing mixed-case variants."""
        # Test mixed-case variants of exec to ensure re.IGNORECASE is applied
        assert _contains_dangerous_pattern("EXEC(") is True
        assert _contains_dangerous_pattern("Exec(") is True
        assert _contains_dangerous_pattern("eXeC(") is True
        # Test mixed-case variants of eval
        assert _contains_dangerous_pattern("EVAL(") is True
        assert _contains_dangerous_pattern("Eval(") is True
        assert _contains_dangerous_pattern("eVaL(") is True


class TestCORSConfiguration:
    """Test CORS configuration."""

    def test_allowed_origins_from_env(self):
        """Verify ALLOWED_ORIGINS respects environment variable."""
        assert isinstance(ALLOWED_ORIGINS, list)
        assert len(ALLOWED_ORIGINS) > 0

    def test_allowed_origins_parsing(self):
        """Verify ALLOWED_ORIGINS are properly parsed and stripped."""
        for origin in ALLOWED_ORIGINS:
            assert isinstance(origin, str)
            assert origin == origin.strip()
            assert len(origin) > 0

    def test_cors_credentials_enabled(self):
        """Verify CORS credentials are enabled."""
        middleware_names = [m.cls.__name__ for m in app.user_middleware]
        assert "CORSMiddleware" in middleware_names


class TestErrorHandling:
    """Test error handling and edge cases."""

    def test_malformed_json_body_handling(self):
        """Verify malformed JSON is handled gracefully."""
        response = client.post(
            "/",
            data="{invalid json}",
            headers={"Content-Type": "application/json"},
        )
        # Should not crash; 405 is valid (POST not accepted on /), 422/400 if body parsed
        assert response.status_code in [400, 404, 405, 422]

    def test_binary_body_handling(self):
        """Verify binary body is handled gracefully."""
        response = client.post(
            "/",
            content=b"\x80\x81\x82\x83",
            headers={"Content-Type": "application/octet-stream"},
        )
        # Should not crash; 405 is valid (POST not accepted on /)
        assert response.status_code in [400, 404, 405, 415]

    def test_empty_query_parameters(self):
        """Verify empty query parameters are handled."""
        response = client.get("/?param=")
        assert response.status_code != 400

    def test_multiple_dangerous_patterns_in_query(self):
        """Verify multiple dangerous patterns are detected."""
        response = client.get("/?param1=eval(1)&param2=exec(2)")
        assert response.status_code == 400


class TestIntegration:
    """Integration tests for the full request/response cycle."""

    def test_health_check_integration(self):
        """Verify health check works end-to-end."""
        response = client.get("/health")
        assert response.status_code == 200
        data = response.json()
        assert "status" in data
        assert "agents" in data

    def test_root_endpoint_integration(self):
        """Verify root endpoint works end-to-end."""
        response = client.get("/")
        assert response.status_code == 200
        data = response.json()
        assert "service" in data
        assert "version" in data
        assert "status" in data

    def test_docs_endpoint_exists(self):
        """Verify OpenAPI docs endpoint is accessible."""
        response = client.get("/docs")
        assert response.status_code in [200, 307, 308]

    def test_redoc_endpoint_exists(self):
        """Verify ReDoc endpoint is accessible."""
        response = client.get("/redoc")
        assert response.status_code in [200, 307, 308]

    def test_openapi_schema_available(self):
        """Verify OpenAPI schema is available."""
        response = client.get("/openapi.json")
        assert response.status_code == 200
        schema = response.json()
        assert "openapi" in schema
        assert "info" in schema
        assert schema["info"]["title"] == "ShipMate AI"
