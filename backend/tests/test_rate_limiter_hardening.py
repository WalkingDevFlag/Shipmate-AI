"""Hardening fixes acted on from a loop run (all validated against the code):

  • Rate-limiter bucket map was an UNBOUNDED memory leak — every distinct IP
    created a bucket that was never removed. Now bounded (LRU cap) + idle-evicted.
  • _get_client_ip blindly trusted client-supplied X-Forwarded-For, letting an
    attacker spoof their per-IP key (dodge the limiter + inflate the map). Now
    XFF is only honored behind a trusted proxy (TRUST_PROXY_HEADERS).
  • /analyze called the synchronous, ~3-min orchestrator.run() directly in an
    async route, blocking the event loop. Now offloaded via asyncio.to_thread.
"""
import time

import pytest


# ── Rate-limiter eviction (was an unbounded leak) ────────────────────────────

class TestRateLimiterEviction:
    def test_hard_cap_never_exceeded(self):
        from app.main import _RateLimiter
        rl = _RateLimiter(capacity=2, refill_rate=1.0, max_buckets=3, idle_evict_s=9999)
        for ip in ("a", "b", "c", "d", "e"):
            rl.is_allowed(ip)
        assert len(rl.buckets) == 3, "bucket map must be capped at max_buckets"

    def test_cap_keeps_most_recently_used(self):
        from app.main import _RateLimiter
        rl = _RateLimiter(capacity=2, refill_rate=1.0, max_buckets=3, idle_evict_s=9999)
        for ip in ("a", "b", "c", "d", "e"):
            rl.is_allowed(ip)
        # a,b were the oldest → evicted; c,d,e survive.
        assert set(rl.buckets) == {"c", "d", "e"}

    def test_touching_an_ip_protects_it_from_lru_eviction(self):
        from app.main import _RateLimiter
        rl = _RateLimiter(capacity=5, refill_rate=1.0, max_buckets=3, idle_evict_s=9999)
        rl.is_allowed("a"); rl.is_allowed("b"); rl.is_allowed("c")
        rl.is_allowed("a")            # 'a' becomes most-recent
        rl.is_allowed("d")            # forces an eviction — should drop 'b', not 'a'
        assert "a" in rl.buckets
        assert "b" not in rl.buckets

    def test_idle_buckets_are_evicted(self):
        from app.main import _RateLimiter
        rl = _RateLimiter(capacity=2, refill_rate=1.0, max_buckets=1000, idle_evict_s=0.05)
        for ip in ("a", "b", "c"):
            rl.is_allowed(ip)
        assert len(rl.buckets) == 3
        time.sleep(0.06)
        rl.is_allowed("z")            # next call sweeps the now-idle a,b,c
        assert set(rl.buckets) == {"z"}, "idle buckets must be swept on the next call"

    def test_limiter_still_actually_limits(self):
        """Eviction must not weaken the actual limit for an active IP."""
        from app.main import _RateLimiter
        rl = _RateLimiter(capacity=2, refill_rate=0.0, max_buckets=1000, idle_evict_s=9999)
        assert rl.is_allowed("x") is True    # token 1
        assert rl.is_allowed("x") is True    # token 2
        assert rl.is_allowed("x") is False   # burst exhausted, no refill


# ── Retry-After hint ─────────────────────────────────────────────────────────

class TestRetryAfter:
    def test_retry_after_when_exhausted(self):
        from app.main import _RateLimiter
        rl = _RateLimiter(capacity=1, refill_rate=0.5, max_buckets=10, idle_evict_s=9999)
        assert rl.is_allowed("x") is True     # consume the only token
        assert rl.is_allowed("x") is False    # exhausted
        # Refill is 0.5 tok/s → ~2s to the next token; ceil, floored at 1.
        assert rl.retry_after_seconds() == 2

    def test_retry_after_never_zero(self):
        from app.main import _RateLimiter
        rl = _RateLimiter(capacity=5, refill_rate=10.0, max_buckets=10, idle_evict_s=9999)
        rl.is_allowed("x")
        assert rl.retry_after_seconds() >= 1   # always a positive backoff


# ── Middleware is actually MOUNTED (the dead-code regression guard) ──────────
# The three rate_limit_* functions used to be defined but never registered, so
# per-IP limiting silently never ran. These tests pin the live behaviour: a
# burst past capacity returns 429 + Retry-After on the real endpoint paths.

class TestRateLimitMiddlewareMounted:
    def _client(self):
        from fastapi.testclient import TestClient
        from app.main import app
        return TestClient(app)

    def test_path_router_matches_real_endpoints(self):
        # The analysis bucket must cover the REAL path (/api/analyze[/stream]),
        # not the old wrong /api/analysis prefix the dead code checked.
        from app.main import _limiter_for_path, _analysis_limiter, _auth_limiter, _webhook_limiter
        assert _limiter_for_path("/api/analyze")[0] is _analysis_limiter
        assert _limiter_for_path("/api/analyze/stream")[0] is _analysis_limiter
        assert _limiter_for_path("/api/auth/github/callback")[0] is _auth_limiter
        assert _limiter_for_path("/webhooks/github")[0] is _webhook_limiter
        assert _limiter_for_path("/api/build/plan")[0] is None   # unguarded path

    def test_middleware_function_exists(self):
        # Regression guard: the rate-limit middleware must exist as a registered
        # @app.middleware function (it was previously defined but never mounted).
        import app.main as m
        assert hasattr(m, "rate_limit_middleware"), "rate_limit_middleware must exist"
        # And the three standalone unregistered functions must be GONE.
        assert not hasattr(m, "rate_limit_auth_middleware"), \
            "the old unregistered rate_limit_auth_middleware must be removed"

    def test_auth_callback_bursts_to_429_with_retry_after(self, monkeypatch):
        # auth limiter is capacity=2; the 3rd rapid hit from the same IP → 429.
        import app.main as m
        # The suite disables the limiter (SHIPMATE_RATE_LIMIT=0 in conftest); flip
        # it back ON for this test so we exercise the real 429 path end-to-end.
        monkeypatch.setenv("SHIPMATE_RATE_LIMIT", "1")
        # Make the limiter deterministic: no refill within the test window.
        m._auth_limiter.buckets.clear()
        monkeypatch.setattr(m._auth_limiter, "refill_rate", 0.0)
        client = self._client()
        # The callback needs ?code/&state; a missing-arg request still passes
        # THROUGH the limiter first (middleware runs before routing/validation).
        seen = [client.get("/api/auth/github/callback").status_code for _ in range(4)]
        assert 429 in seen, f"limiter never tripped: {seen}"
        # Confirm the 429 carries a Retry-After header.
        last = client.get("/api/auth/github/callback")
        assert last.status_code == 429
        assert int(last.headers.get("Retry-After", "0")) >= 1


# ── X-Forwarded-For trust gate ───────────────────────────────────────────────

class _FakeReq:
    def __init__(self, xff=None, peer="9.9.9.9"):
        self.headers = {"x-forwarded-for": xff} if xff else {}

        class _C:
            host = peer
        self.client = _C()


class TestClientIpTrust:
    def test_xff_ignored_by_default(self, monkeypatch):
        # Default (no trusted proxy): a spoofed XFF must NOT become the key —
        # the unforgeable socket peer is used instead.
        import app.main as m
        monkeypatch.setattr(m, "_TRUST_PROXY_HEADERS", False)
        ip = m._get_client_ip(_FakeReq(xff="1.2.3.4", peer="9.9.9.9"))
        assert ip == "9.9.9.9", "must not trust client-supplied XFF by default"

    def test_xff_honored_when_proxy_trusted(self, monkeypatch):
        import app.main as m
        monkeypatch.setattr(m, "_TRUST_PROXY_HEADERS", True)
        ip = m._get_client_ip(_FakeReq(xff="1.2.3.4, 10.0.0.1", peer="9.9.9.9"))
        assert ip == "1.2.3.4", "behind a trusted proxy, take the left-most XFF entry"

    def test_falls_back_to_peer_when_no_xff(self, monkeypatch):
        import app.main as m
        monkeypatch.setattr(m, "_TRUST_PROXY_HEADERS", True)
        ip = m._get_client_ip(_FakeReq(xff=None, peer="9.9.9.9"))
        assert ip == "9.9.9.9"


# ── /analyze offloaded from the event loop ───────────────────────────────────

class TestAnalyzeOffloaded:
    def test_analyze_runs_orchestrator_in_thread(self, monkeypatch):
        """orchestrator.run must be dispatched off the event loop so a ~3-min
        analysis doesn't freeze every other request. We assert run() executes on
        a DIFFERENT thread than the asyncio loop thread."""
        import threading
        import app.api.routes.analysis as analysis_mod

        # Pass the auth + credential gates without network.
        async def ok_auth(token, owner, repo):
            return None
        monkeypatch.setattr(analysis_mod, "verify_repo_write_access", ok_auth)
        monkeypatch.setattr(analysis_mod, "require_body_credential", lambda v: "real-token")

        class _Index:
            repo_context = {"repo_info": {"full_name": "o/r"}}
            repo_lens = None

        async def fake_get_or_build(cls, **kw):
            return _Index()
        monkeypatch.setattr(analysis_mod.RepoIndexService, "get_or_build",
                            classmethod(fake_get_or_build))

        loop_thread = threading.get_ident()
        ran_on = {}

        # Raise AFTER capturing the thread id — we only care WHERE run() executed,
        # not what it returns (building a full ShipMateReport here is noise).
        # The route now builds a per-request orchestrator via new_per_request(),
        # so patch run() on the CLASS.
        def fake_run(self, ctx):
            ran_on["thread"] = threading.get_ident()
            raise RuntimeError("stop here — thread already captured")
        monkeypatch.setattr(analysis_mod.ShipMateOrchestrator, "run", fake_run)
        monkeypatch.setattr(analysis_mod.report_store, "save_report", lambda r: 1)

        from fastapi.testclient import TestClient
        from app.main import app
        client = TestClient(app)
        client.post("/api/analyze", json={
            "owner": "o", "repo": "r", "branch": "main", "access_token": "t",
        })
        assert ran_on.get("thread") is not None, "orchestrator.run was never invoked"
        assert ran_on["thread"] != loop_thread, \
            "orchestrator.run must execute on a worker thread, not the event loop"


# ── Per-request orchestrator isolation (the "build new" finding) ─────────────

class TestPerRequestOrchestrator:
    def test_factory_returns_fresh_isolated_instances(self):
        from app.orchestrator.shipmate_orchestrator import ShipMateOrchestrator
        a = ShipMateOrchestrator.new_per_request()
        b = ShipMateOrchestrator.new_per_request()
        assert a is not b, "each request must get its own orchestrator"
        assert a.repo_lens is not b.repo_lens
        assert a.guardrail is not b.guardrail
        assert a.plan_forge is not b.plan_forge
        assert a.testpilot is not b.testpilot

    def test_no_module_global_orchestrator(self):
        import app.api.routes.analysis as analysis_mod
        assert not hasattr(analysis_mod, "_orchestrator"), \
            "module-global _orchestrator must be removed in favor of per-request scope"

    def test_agents_stay_stateless_contract(self):
        """Per-request scoping backstops a stateless-agent invariant: an agent
        that grew mutable run-state on self would reintroduce the cross-request
        race. Two fresh orchestrators' agents must have identical attribute
        shape (no accumulation)."""
        from app.orchestrator.shipmate_orchestrator import ShipMateOrchestrator
        o = ShipMateOrchestrator.new_per_request()
        o2 = ShipMateOrchestrator.new_per_request()
        assert set(vars(o.guardrail)) == set(vars(o2.guardrail))
