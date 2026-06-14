"""Consolidated CORS tests for ShipMate AI backend.

Covers:
- Unit tests for _validate_origin (SEC-004): scheme validation, wildcard rejection,
  empty-host rejection, valid HTTP/HTTPS origins.
- Unit tests for _is_origin_allowed: exact-match semantics, prefix spoofing,
  subdomain spoofing, trailing-slash edge cases, empty string, wildcard string.
- Env-var parsing: ALLOWED_ORIGINS with malformed (empty-entry) values.
- Integration tests against the real ASGI app: preflight and simple-request
  scenarios for both allowed and unlisted/spoofed origins.
"""
import os
import sys
import importlib

import pytest
from starlette.testclient import TestClient

from app.main import app, _is_origin_allowed, _validate_origin


# ---------------------------------------------------------------------------
# Unit tests: _validate_origin
# ---------------------------------------------------------------------------

class TestValidateOrigin:
    """_validate_origin must accept valid origins and raise ValueError for bad ones."""

    def test_valid_https_origin_is_accepted(self):
        assert _validate_origin("https://app.shipmate.ai") == "https://app.shipmate.ai"

    def test_valid_http_localhost_is_accepted(self):
        assert _validate_origin("http://localhost:5173") == "http://localhost:5173"

    def test_valid_http_with_ip_and_port_is_accepted(self):
        assert _validate_origin("http://127.0.0.1:3000") == "http://127.0.0.1:3000"

    def test_trailing_space_stripped_origin_is_valid(self):
        # Simulates what the ALLOWED_ORIGINS list comprehension does: strip before validate
        origin = "  https://app.shipmate.ai  ".strip()
        assert _validate_origin(origin) == "https://app.shipmate.ai"

    def test_bare_wildcard_is_rejected(self):
        with pytest.raises(ValueError, match="wildcard"):
            _validate_origin("*")

    def test_wildcard_in_subdomain_is_rejected(self):
        with pytest.raises(ValueError, match="wildcard"):
            _validate_origin("https://*.shipmate.ai")

    def test_ftp_scheme_is_rejected(self):
        with pytest.raises(ValueError, match="invalid scheme"):
            _validate_origin("ftp://shipmate.ai")

    def test_no_scheme_is_rejected(self):
        with pytest.raises(ValueError):
            _validate_origin("shipmate.ai")

    def test_empty_netloc_is_rejected(self):
        with pytest.raises(ValueError, match="empty host"):
            _validate_origin("https://")


# ---------------------------------------------------------------------------
# Unit tests: _is_origin_allowed
# ---------------------------------------------------------------------------

class TestIsOriginAllowed:
    """_is_origin_allowed must use exact-match semantics only."""

    def test_exact_allowed_origin_is_accepted(self):
        assert _is_origin_allowed("http://localhost:5173") is True

    def test_spoofed_prefix_origin_is_rejected(self):
        """http://localhost:5173.evil.com must NOT be treated as allowed."""
        assert _is_origin_allowed("http://localhost:5173.evil.com") is False

    def test_subdomain_of_allowed_origin_is_rejected(self):
        assert _is_origin_allowed("http://evil.localhost:5173") is False

    def test_allowed_origin_with_trailing_slash_is_rejected(self):
        """Trailing slash changes the origin string — must not match."""
        assert _is_origin_allowed("http://localhost:5173/") is False

    def test_empty_string_is_rejected(self):
        assert _is_origin_allowed("") is False

    def test_wildcard_string_is_rejected(self):
        assert _is_origin_allowed("*") is False


# ---------------------------------------------------------------------------
# Env-var parsing: ALLOWED_ORIGINS
# ---------------------------------------------------------------------------

class TestAllowedOriginsParsing:
    """ALLOWED_ORIGINS env-var parsing must filter out empty strings from malformed values."""

    def test_malformed_env_var_excludes_empty_strings(self):
        """
        When ALLOWED_ORIGINS contains leading/trailing commas or consecutive commas
        (e.g. ',http://localhost:5173,,http://localhost:3000,'), the resulting list
        must contain no empty strings.  An empty-string origin would act as a wildcard
        in CORSMiddleware, silently breaking the security model.
        """
        malformed_origins = ",http://localhost:5173,,http://localhost:3000,"

        with pytest.MonkeyPatch.context() as mp:
            mp.setenv("ALLOWED_ORIGINS", malformed_origins)
            import app.main as main_module
            importlib.reload(main_module)

            assert "" not in main_module.ALLOWED_ORIGINS
            assert "http://localhost:5173" in main_module.ALLOWED_ORIGINS
            assert "http://localhost:3000" in main_module.ALLOWED_ORIGINS
            assert len(main_module.ALLOWED_ORIGINS) == 2


# ---------------------------------------------------------------------------
# Fixtures for integration tests
# ---------------------------------------------------------------------------

@pytest.fixture(scope="module")
def integration_client():
    """TestClient backed by the real app with raise_server_exceptions=True."""
    with TestClient(app, raise_server_exceptions=True) as c:
        yield c


@pytest.fixture()
def allowed_origin():
    return "http://localhost:5173"


@pytest.fixture()
def unlisted_origin():
    return "https://evil.example.com"


@pytest.fixture()
def controlled_client(allowed_origin, monkeypatch):
    """TestClient with ALLOWED_ORIGINS forced to a single known origin via env-var patch."""
    monkeypatch.setenv("ALLOWED_ORIGINS", allowed_origin)

    for mod_name in list(sys.modules.keys()):
        if mod_name == "app.main" or mod_name.startswith("app.main."):
            del sys.modules[mod_name]

    from app.main import app as patched_app  # noqa: PLC0415
    return TestClient(patched_app, raise_server_exceptions=True)


# ---------------------------------------------------------------------------
# Integration: unlisted origins must not receive CORS headers
# ---------------------------------------------------------------------------

class TestCORSUnlistedOrigin:
    """Requests from origins not in the allowlist must not receive CORS headers."""

    def test_preflight_unlisted_origin_no_acao_header(
        self, controlled_client: TestClient, unlisted_origin: str
    ):
        response = controlled_client.options(
            "/health",
            headers={
                "Origin": unlisted_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in response.headers, (
            f"Unlisted origin '{unlisted_origin}' should not receive "
            "Access-Control-Allow-Origin header."
        )

    def test_simple_request_unlisted_origin_no_acao_header(
        self, controlled_client: TestClient, unlisted_origin: str
    ):
        response = controlled_client.get(
            "/health", headers={"Origin": unlisted_origin}
        )
        assert "access-control-allow-origin" not in response.headers, (
            f"Unlisted origin '{unlisted_origin}' should not receive "
            "Access-Control-Allow-Origin header."
        )


# ---------------------------------------------------------------------------
# Integration: allowed origins must receive the correct CORS header
# ---------------------------------------------------------------------------

class TestCORSAllowedOrigin:
    """Requests from origins in the allowlist must receive the correct CORS header."""

    def test_preflight_allowed_origin_returns_acao_header(
        self, controlled_client: TestClient, allowed_origin: str
    ):
        response = controlled_client.options(
            "/health",
            headers={
                "Origin": allowed_origin,
                "Access-Control-Request-Method": "GET",
            },
        )
        acao = response.headers.get("access-control-allow-origin", "")
        assert acao == allowed_origin, (
            f"Expected Access-Control-Allow-Origin: {allowed_origin}, got: {acao!r}"
        )

    def test_simple_request_allowed_origin_returns_acao_header(
        self, controlled_client: TestClient, allowed_origin: str
    ):
        response = controlled_client.get(
            "/health", headers={"Origin": allowed_origin}
        )
        acao = response.headers.get("access-control-allow-origin", "")
        assert acao == allowed_origin, (
            f"Expected Access-Control-Allow-Origin: {allowed_origin}, got: {acao!r}"
        )


# ---------------------------------------------------------------------------
# Integration: spoofed-origin attacks against the real app
# ---------------------------------------------------------------------------

class TestCORSMiddlewareIntegration:
    """Spoofed origins must not receive CORS headers or be reflected as wildcards."""

    def test_spoofed_origin_not_reflected_in_response(self, integration_client):
        """Access-Control-Allow-Origin must NOT be set to the spoofed origin."""
        spoofed = "http://localhost:5173.evil.com"
        response = integration_client.get("/health", headers={"Origin": spoofed})
        acao = response.headers.get("access-control-allow-origin", "")
        assert acao != spoofed

    def test_spoofed_origin_not_reflected_as_wildcard(self, integration_client):
        """The response must not fall back to a wildcard when origin is spoofed."""
        spoofed = "http://localhost:5173.evil.com"
        response = integration_client.get("/health", headers={"Origin": spoofed})
        acao = response.headers.get("access-control-allow-origin", "")
        assert acao != "*"

    def test_legitimate_origin_is_reflected(self, integration_client):
        """A valid allowlisted origin must be echoed back."""
        valid = "http://localhost:5173"
        response = integration_client.get("/health", headers={"Origin": valid})
        assert response.headers.get("access-control-allow-origin") == valid

    def test_spoofed_preflight_returns_4xx(self, integration_client):
        """OPTIONS preflight from a spoofed origin must be rejected (400 or 403)."""
        spoofed = "http://localhost:5173.evil.com"
        response = integration_client.options(
            "/health",
            headers={
                "Origin": spoofed,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code in (400, 403)

    def test_legitimate_preflight_is_not_rejected(self, integration_client):
        """OPTIONS preflight from a valid origin must not be rejected."""
        valid = "http://localhost:5173"
        response = integration_client.options(
            "/health",
            headers={
                "Origin": valid,
                "Access-Control-Request-Method": "GET",
            },
        )
        assert response.status_code != 403
