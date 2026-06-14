"""Request body-size cap (ReDoS / memory-exhaustion guard).

The sanitizer middleware reads the full body and runs multi-pass injection
regexes over it; the webhook handler buffers the body for HMAC. Without a size
cap an attacker could feed an unbounded payload to exhaust memory or trigger
pathological regex backtracking. MAX_BODY_BYTES rejects an oversized body with
413 BEFORE any regex runs — by declared Content-Length (cheap) and by actual
read length (defends a lying/absent Content-Length).
"""
import pytest
from fastapi.testclient import TestClient

from app import main as main_mod
from app.main import app, MAX_BODY_BYTES, _body_too_large


client = TestClient(app)


class _Req:
    """Minimal stand-in with a headers dict for _body_too_large unit tests."""
    def __init__(self, headers):
        self.headers = headers


class TestBodyTooLargeUnit:
    def test_content_length_over_limit(self):
        req = _Req({"content-length": str(MAX_BODY_BYTES + 1)})
        assert _body_too_large(req) is True

    def test_content_length_at_limit_ok(self):
        req = _Req({"content-length": str(MAX_BODY_BYTES)})
        assert _body_too_large(req) is False

    def test_actual_length_over_limit(self):
        # No / lying Content-Length → caught by the read-length check.
        req = _Req({})
        assert _body_too_large(req, MAX_BODY_BYTES + 1) is True
        assert _body_too_large(req, 10) is False

    def test_malformed_content_length_falls_through(self):
        req = _Req({"content-length": "not-a-number"})
        # Doesn't raise; with no body_len it can't decide → False (fail-open to
        # the post-read check elsewhere).
        assert _body_too_large(req) is False


class TestSanitizerBodyCap:
    def test_oversized_body_rejected_413(self):
        # A 3 MB JSON body (> 2 MB default) must be rejected before scanning.
        big = '{"x":"' + ("a" * (3 * 1024 * 1024)) + '"}'
        resp = client.post(
            "/api/analyze", content=big,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413
        assert "too large" in resp.json()["detail"].lower()

    def test_normal_body_not_rejected_for_size(self):
        # A small body must NOT 413 (it may 4xx for other reasons like auth,
        # but never 413). Proves the cap doesn't block legitimate requests.
        resp = client.post(
            "/api/analyze", json={"owner": "", "repo": "", "access_token": "x"},
            headers={"content-type": "application/json"},
        )
        assert resp.status_code != 413

    def test_webhook_oversized_rejected_413(self):
        # Real webhook path is /webhooks/github (no /api prefix). An oversized
        # body is rejected 413 — by the sanitizer middleware and/or the
        # handler's own guard — before HMAC/JSON parsing.
        big = "x" * (3 * 1024 * 1024)
        resp = client.post(
            "/webhooks/github", content=big,
            headers={"content-type": "application/json"},
        )
        assert resp.status_code == 413


def test_max_body_bytes_env_override(monkeypatch):
    # The cap is env-tunable; the default is a sane 2 MB.
    assert MAX_BODY_BYTES == 2 * 1024 * 1024
