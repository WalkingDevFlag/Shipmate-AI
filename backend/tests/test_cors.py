"""Integration tests for CORS policy (SEC-001).

Ensures that:
- Requests from unlisted origins do NOT receive CORS headers.
- Requests from allowed origins DO receive the correct CORS header.
- The wildcard '*' is never accepted as an allowed origin.
"""
import os
import importlib
import sys
import pytest
from fastapi.testclient import TestClient


ALLOWED = "http://localhost:5173"
UNLISTED = "https://evil.example.com"


@pytest.fixture()
def client(monkeypatch):
    """Return a TestClient whose app is initialised with a controlled allowlist."""
    monkeypatch.setenv("ALLOWED_ORIGINS", ALLOWED)

    # Force re-import so the module picks up the patched env var.
    for mod_name in list(sys.modules.keys()):
        if "app.main" in mod_name or mod_name == "app.main":
            del sys.modules[mod_name]

    from app.main import app  # noqa: PLC0415
    return TestClient(app, raise_server_exceptions=True)


def test_allowed_origin_receives_cors_header(client):
    """A request from a listed origin must echo that origin in the ACAO header."""
    response = client.get(
        "/health",
        headers={"Origin": ALLOWED},
    )
    assert response.status_code == 200
    acao = response.headers.get("access-control-allow-origin")
    assert acao == ALLOWED, f"Expected '{ALLOWED}', got '{acao}'"


def test_unlisted_origin_receives_no_cors_header(client):
    """A request from an unlisted origin must NOT receive an ACAO header."""
    response = client.get(
        "/health",
        headers={"Origin": UNLISTED},
    )
    # The endpoint itself is still reachable (CORS is a browser-enforced policy),
    # but the ACAO header must be absent so browsers will block the response.
    assert "access-control-allow-origin" not in response.headers, (
        "Unlisted origin should not receive Access-Control-Allow-Origin header"
    )


def test_preflight_unlisted_origin_no_cors_header(client):
    """An OPTIONS preflight from an unlisted origin must not receive ACAO header."""
    response = client.options(
        "/health",
        headers={
            "Origin": UNLISTED,
            "Access-Control-Request-Method": "GET",
        },
    )
    assert "access-control-allow-origin" not in response.headers, (
        "Preflight from unlisted origin should not receive Access-Control-Allow-Origin header"
    )


def test_wildcard_origin_raises_at_startup(monkeypatch):
    """Configuring '*' as an allowed origin must raise RuntimeError at startup."""
    monkeypatch.setenv("ALLOWED_ORIGINS", "*")

    for mod_name in list(sys.modules.keys()):
        if "app.main" in mod_name or mod_name == "app.main":
            del sys.modules[mod_name]

    with pytest.raises(RuntimeError, match="ALLOWED_ORIGINS must not contain"):
        import app.main  # noqa: F401, PLC0415
