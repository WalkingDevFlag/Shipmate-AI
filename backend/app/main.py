from dotenv import load_dotenv
load_dotenv()

import os
import re
import json
import time
import math
import hmac
import hashlib
import logging
import unicodedata
from collections import OrderedDict
from io import BytesIO
from typing import Any
from urllib.parse import urlparse

logger = logging.getLogger("shipmate.main")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
import uvicorn

from app.api.routes.auth import router as auth_router
from app.api.routes.analysis import router as analysis_router
from app.api.routes.actuate import router as actuate_router
from app.api.routes.watcher import router as watcher_router
from app.api.routes.branches import router as branches_router
from app.api.routes.findings import router as findings_router
from app.api.routes.auto_fix import router as auto_fix_router
from app.api.routes.build import router as build_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.research import router as research_router

logger = logging.getLogger("shipmate")

# ---------------------------------------------------------------------------
# Rate Limiting: Token Bucket Implementation
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple token bucket for rate limiting."""

    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = float(capacity)
        self.last_refill = time.time()

    def allow_request(self) -> bool:
        now = time.time()
        elapsed = now - self.last_refill
        self.tokens = min(self.capacity, self.tokens + elapsed * self.refill_rate)
        self.last_refill = now
        if self.tokens >= 1.0:
            self.tokens -= 1.0
            return True
        return False

    def retry_after_seconds(self) -> int:
        """Whole seconds until the next token is available (for Retry-After).

        Assumes a refill just happened (allow_request() updates self.tokens),
        so this reflects the wait from now. Always >= 1 so a client never
        retries instantly into another 429."""
        if self.refill_rate <= 0:
            return 1
        deficit = max(0.0, 1.0 - self.tokens)
        return max(1, math.ceil(deficit / self.refill_rate))


class _RateLimiter:
    """Per-IP rate limiter using token buckets.

    Bucket eviction (was an unbounded memory leak): every distinct client IP
    created a bucket that was NEVER removed, so steady traffic — or a flood of
    spoofed X-Forwarded-For values — grew `buckets` without bound. We now (a)
    lazily drop buckets that have sat idle past an eviction window (a full
    bucket is indistinguishable from a fresh one, so an idle entry is pure
    waste), and (b) hard-cap the dict size, evicting the least-recently-seen
    entry when full. Both are O(1)-amortized and need no background task."""

    def __init__(self, capacity: int, refill_rate: float,
                 max_buckets: int = 10_000, idle_evict_s: float = 3600.0):
        """Initialize rate limiter.

        Args:
            capacity: Burst size (max tokens per bucket).
            refill_rate: Tokens per second.
            max_buckets: Hard ceiling on tracked IPs (LRU-evict past this).
            idle_evict_s: Drop a bucket untouched for this many seconds.
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.max_buckets = max_buckets
        self.idle_evict_s = idle_evict_s
        # OrderedDict so we can evict the least-recently-used IP in O(1).
        self.buckets: "OrderedDict[str, _TokenBucket]" = OrderedDict()

    def _evict_idle(self, now: float) -> None:
        # Drop entries that have gone idle past the window. Buckets are kept in
        # last-seen order (move_to_end on touch), so the stale ones are always
        # at the front — stop at the first still-fresh entry.
        while self.buckets:
            _ip, bucket = next(iter(self.buckets.items()))
            if now - bucket.last_refill > self.idle_evict_s:
                self.buckets.popitem(last=False)
            else:
                break

    def is_allowed(self, client_ip: str) -> bool:
        """Check if a request from client_ip is allowed.

        Returns True if allowed, False if rate limit exceeded.
        """
        now = time.time()
        self._evict_idle(now)
        bucket = self.buckets.get(client_ip)
        if bucket is None:
            # Make room BEFORE inserting so the dict never exceeds max_buckets:
            # evict least-recently-used until there's a free slot for the new IP.
            while len(self.buckets) >= self.max_buckets:
                self.buckets.popitem(last=False)
            bucket = _TokenBucket(self.capacity, self.refill_rate)
            self.buckets[client_ip] = bucket
        else:
            # Mark as most-recently-used.
            self.buckets.move_to_end(client_ip)
        self._last_checked = bucket
        return bucket.allow_request()

    def retry_after_seconds(self) -> int:
        """Retry-After hint (seconds) for the bucket touched by the most recent
        is_allowed() call. Falls back to a 1s floor if nothing was checked yet."""
        bucket = getattr(self, "_last_checked", None)
        return bucket.retry_after_seconds() if bucket is not None else 1


# Auth callback: 10 requests per minute per IP (burst of 2)
_auth_limiter = _RateLimiter(capacity=2, refill_rate=10.0 / 60.0)
# Analysis endpoint: 30 requests per minute per IP (burst of 5)
_analysis_limiter = _RateLimiter(capacity=5, refill_rate=30.0 / 60.0)
# Webhook endpoint: 60 requests per minute per IP (burst of 10)
_webhook_limiter = _RateLimiter(capacity=10, refill_rate=60.0 / 60.0)


# X-Forwarded-For is CLIENT-CONTROLLED and must only be trusted when the
# request actually arrives through a known reverse proxy that appends it.
# Trusting it unconditionally (the old behavior) let anyone spoof their per-IP
# rate-limit key — send a random XFF per request and every request looks like a
# brand-new IP, defeating the limiter AND inflating the bucket map. We only honor
# XFF when TRUST_PROXY_HEADERS is enabled (set it true behind Azure Container
# Apps / any trusted ingress that sets the header); otherwise we use the real
# socket peer address, which a client cannot forge.
_TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in ("1", "true", "yes")


def _get_client_ip(request: Request) -> str:
    """Resolve the client IP for rate-limiting.

    When TRUST_PROXY_HEADERS is set (deployed behind a trusted proxy that
    populates X-Forwarded-For), take the left-most XFF entry. Otherwise — and
    by default — use the unforgeable socket peer address. Never trust an
    attacker-supplied header to key security controls."""
    if _TRUST_PROXY_HEADERS:
        forwarded_for = request.headers.get("x-forwarded-for")
        if forwarded_for:
            # X-Forwarded-For can carry multiple IPs; the original client is left-most.
            first = forwarded_for.split(",")[0].strip()
            if first:
                return first
    if request.client:
        return request.client.host
    return "unknown"


# Map a request path to the limiter that guards it. A path matches when it
# equals an entry or starts with it + "/" (so /api/analyze AND /api/analyze/stream
# both fall under the analysis bucket). Ordered most-specific-first; first match
# wins. NOTE: these limiters are mounted via @app.middleware below — earlier they
# were defined but never registered, so per-IP limiting silently never ran.
_RATE_LIMIT_ROUTES: tuple = (
    ("/api/auth/github/callback", lambda: _auth_limiter, "Too many authentication attempts."),
    ("/api/analyze",              lambda: _analysis_limiter, "Too many analysis requests."),
    ("/webhooks/github",          lambda: _webhook_limiter, "Too many webhook requests."),
)


def _limiter_for_path(path: str):
    """Return (limiter, message) for the first route prefix that guards `path`,
    or (None, None) when the path isn't rate-limited."""
    for prefix, limiter_fn, message in _RATE_LIMIT_ROUTES:
        if path == prefix or path.startswith(prefix + "/"):
            return limiter_fn(), message
    return None, None


# ---------------------------------------------------------------------------
# Startup: Enforce HTTPS for all configured service endpoint URLs
# ---------------------------------------------------------------------------

_ENDPOINT_ENV_VARS = [
    "BEDROCK_ENDPOINT",
    "API_BASE_URL",
    "INTERNAL_API_URL",
    "GITHUB_API_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_URL",
]

_LOCAL_HOSTS = frozenset([
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
])


def _validate_endpoint_urls() -> None:
    """Refuse to start if any configured endpoint URL uses plain HTTP with a non-local host."""
    for var in _ENDPOINT_ENV_VARS:
        value = os.getenv(var, "").strip()
        if not value:
            continue
        parsed = urlparse(value)
        if parsed.scheme == "http":
            host = (parsed.hostname or "").lower()
            if host not in _LOCAL_HOSTS:
                raise ValueError(
                    f"Environment variable {var!r} is set to a plain HTTP URL "
                    f"({value!r}). Auth tokens must not be transmitted over "
                    "unencrypted connections. Change the URL scheme to 'https://' "
                    "before starting the application."
                )


_validate_endpoint_urls()

# ---------------------------------------------------------------------------
# Input Sanitization
#
# The dangerous-pattern set, _contains_dangerous_pattern(), and the live
# middleware all live further down (search "_DANGEROUS_PATTERNS: list").
# An earlier duplicate set + an unregistered middleware variant
# (input_sanitization_middleware / _BodyReplayRequest / the recursive JSON
# scanner) used to sit here and silently SHADOWED those real definitions —
# editing them had no effect. Removed; only the content-type helper below is
# shared with the live middleware.
# ---------------------------------------------------------------------------

# Content-type prefixes that carry text payloads and must be scanned.
_TEXT_CONTENT_TYPES = (
    "application/json",
    "application/x-www-form-urlencoded",
    "multipart/form-data",
    "text/plain",
    "text/",
)


def _is_text_content_type(content_type: str) -> bool:
    """Return True when the content-type indicates a text-based body."""
    ct_lower = content_type.lower()
    return any(ct_lower.startswith(prefix) or prefix in ct_lower for prefix in _TEXT_CONTENT_TYPES)


# ---------------------------------------------------------------------------
# CORS origin allowlist
# In production set ALLOWED_ORIGINS to a comma-separated list of origins, e.g.:
#   ALLOWED_ORIGINS=https://app.shipmate.ai
# Wildcards are intentionally not supported (incompatible with allow_credentials=True).
# ---------------------------------------------------------------------------

def _validate_origin(origin: str) -> str:
    if "*" in origin:
        raise ValueError(
            f"CORS origin '{origin}' contains a wildcard character, which is not "
            "allowed when allow_credentials=True."
        )
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"CORS origin '{origin}' has an invalid scheme '{parsed.scheme}'."
        )
    if not parsed.netloc:
        raise ValueError(
            f"CORS origin '{origin}' has an empty host/netloc."
        )
    return origin


_raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:5174,http://localhost:3000,http://127.0.0.1:5173",
)
ALLOWED_ORIGINS: list[str] = [
    _validate_origin(origin.strip())
    for origin in _raw_origins.split(",")
    if origin.strip()
]


def _is_origin_allowed(origin: str) -> bool:
    """Exact-match check: is *origin* on the CORS allowlist? (SEC-004)

    Deliberately strict — no prefix, suffix, or subdomain matching. This is
    the guard that prevents spoofed origins a naive ``startswith``/substring
    check would wrongly accept:

    - ``http://localhost:5173.evil.com``  (prefix attack)  -> False
    - ``http://evil.localhost:5173``      (subdomain)       -> False
    - ``http://localhost:5173/``          (trailing slash)  -> False
    - ``""``                              (empty)           -> False
    - ``"*"``                             (wildcard)        -> False

    A wildcard is never allowed because ``allow_credentials=True`` is
    incompatible with ``*``. Returns True only for a byte-for-byte member of
    ALLOWED_ORIGINS.
    """
    if not origin or "*" in origin:
        return False
    return origin in ALLOWED_ORIGINS

# ---------------------------------------------------------------------------
# Input sanitization — block code-injection patterns before they reach routes
# ---------------------------------------------------------------------------
_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"eval\s*\(",             re.IGNORECASE),
    re.compile(r"exec\s*\(",             re.IGNORECASE),
    re.compile(r"__import__\s*\(",       re.IGNORECASE),
    re.compile(r"__builtins__",          re.IGNORECASE),
    re.compile(r"__globals__",           re.IGNORECASE),
    re.compile(r"__locals__",            re.IGNORECASE),
    re.compile(r"compile\s*\(",          re.IGNORECASE),
    re.compile(r"importlib\.import_module", re.IGNORECASE),
    re.compile(r"subprocess\.",          re.IGNORECASE),
    re.compile(r"os\.system\s*\(",       re.IGNORECASE),
    re.compile(r"os\.popen\s*\(",        re.IGNORECASE),
]

_SKIP_HEADERS = {"authorization", "cookie"}

# Max request body the sanitizer will read+scan. A body larger than this is
# rejected with 413 BEFORE any regex runs, so an attacker can't feed an
# unbounded payload through the (multi-pass) injection-pattern scan to exhaust
# memory or trigger pathological regex backtracking (ReDoS). 2 MB is far above
# any legitimate ShipMate request (the largest is a CoderOutput-bearing actuate,
# well under this). Override with SHIPMATE_MAX_BODY_BYTES.
MAX_BODY_BYTES = int(os.getenv("SHIPMATE_MAX_BODY_BYTES", str(2 * 1024 * 1024)))


def _body_too_large(request: "Request", body_len: int | None = None) -> bool:
    """True if the request body exceeds MAX_BODY_BYTES. Checks the declared
    Content-Length first (cheap, lets us reject before reading), then the actual
    read length when provided. A malformed/absent Content-Length falls through
    to the post-read check."""
    cl = request.headers.get("content-length")
    if cl is not None:
        try:
            if int(cl) > MAX_BODY_BYTES:
                return True
        except (TypeError, ValueError):
            pass
    return body_len is not None and body_len > MAX_BODY_BYTES


def _normalize_for_scanning(text: str, max_passes: int = 3) -> str:
    """Defeat common blocklist-evasion encodings before pattern matching.

    An attacker can hide `eval(` as `eval%2528` (double URL-encode), `ev%61l(`
    (partial), or via Unicode compatibility forms. We:
      1. Recursively URL-decode until the string stops changing (bounded passes,
         so a pathological input can't loop) — catches multi-layer %-encoding.
      2. Apply Unicode NFKC normalization — folds compatibility/full-width
         variants (e.g. ﹙ -> '(') to their canonical ASCII so the regexes match.
    Returns the most-decoded form; callers scan BOTH this and the raw text."""
    from urllib.parse import unquote_plus

    prev = text
    for _ in range(max_passes):
        decoded = unquote_plus(prev)
        if decoded == prev:
            break
        prev = decoded
    try:
        prev = unicodedata.normalize("NFKC", prev)
    except Exception:
        pass
    return prev


def _contains_dangerous_pattern(text: str) -> bool:
    """Return True if *text* matches any known code-injection pattern.

    Scans BOTH the raw text and an encoding-normalized form (recursive
    URL-decode + Unicode NFKC) so blocklist-evasion via %-encoding or Unicode
    compatibility variants can't slip a payload past the regexes."""
    if any(p.search(text) for p in _DANGEROUS_PATTERNS):
        return True
    normalized = _normalize_for_scanning(text)
    if normalized != text and any(p.search(normalized) for p in _DANGEROUS_PATTERNS):
        return True
    return False


from contextlib import asynccontextmanager


@asynccontextmanager
async def _lifespan(app: "FastAPI"):
    """Startup: initialize the inflight-registry sqlite (creates tables) and
    re-spawn CIWatcher supervisors for any PR still being watched when the
    backend last stopped (uvicorn --reload restarts on every code change).
    Shutdown: nothing to flush — sqlite commits are synchronous per write."""
    # --- startup ---
    try:
        # Import every module that registers a sqlite store so init_all() below
        # creates ALL of them — the route imports cover inflight/reports/oauth;
        # these are the memory/observability stores added for the architecture
        # work (run traces, semantic finding memory, Coder lessons, capability
        # posture, persistent repo-index L2). Importing is enough — each module
        # calls sqlite_store.register() at import time.
        from app.services import (  # noqa: F401
            run_trace, finding_memory, coder_lessons,
            capability_store, repo_index_store,
        )
        # One call creates every registered sqlite store with the shared
        # connection/PRAGMA/location policy.
        from app.services import sqlite_store as _store
        _store.init_all()
    except Exception as e:  # pragma: no cover - startup best-effort
        logger.warning("sqlite_store init_all failed: %s", e)
    try:
        from app.services.ci_watcher import CIWatcher
        resumed = CIWatcher.resume_from_db()
        if resumed:
            logger.info("resumed %d CI watcher(s) after restart", resumed)
    except Exception as e:  # pragma: no cover
        logger.warning("CIWatcher resume failed: %s", e)

    yield
    # --- shutdown --- (no-op; sqlite is durable per-commit)


# Production hardening flag. In production we disable the interactive API docs
# (/docs, /redoc) and the OpenAPI schema (/openapi.json) so the full route map,
# request/response models, and "Try it out" console aren't exposed to anonymous
# visitors. Local/dev keeps them on for convenience. Driven by ENVIRONMENT
# (already present in .env / .env.example); "production" or "prod" => hardened.
_ENVIRONMENT = os.getenv("ENVIRONMENT", "development").strip().lower()
_IS_PRODUCTION = _ENVIRONMENT in ("production", "prod")

# ---------------------------------------------------------------------------
# GitHub webhook HMAC-SHA256 signature verification
# ---------------------------------------------------------------------------

def _verify_github_webhook_signature(body: bytes, header_sig: str) -> bool:
    """Constant-time HMAC-SHA256 comparison for GitHub webhook payloads."""
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        return False
    mac = hmac.new(secret.encode(), body, hashlib.sha256)
    expected = "sha256=" + mac.hexdigest()
    return hmac.compare_digest(expected, header_sig)


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

_is_dev = os.getenv("ENVIRONMENT", "production").lower() == "development"
app = FastAPI(
    title="ShipMate AI",
    description="AI-native multi-agent release readiness platform",
    version="2.0.0",
    docs_url=None if _IS_PRODUCTION else "/docs",
    redoc_url=None if _IS_PRODUCTION else "/redoc",
    openapi_url=None if _IS_PRODUCTION else "/openapi.json",
    lifespan=_lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


# Per-IP rate limiting is mounted as a SINGLE @app.middleware
# (rate_limit_middleware, defined below) that dispatches to the right bucket via
# _limiter_for_path. It's registered LAST so it ends up OUTERMOST — over-limit
# requests get 429'd before any sanitization/routing work. The three separate
# _rate_limit_* wrappers that used to live here were dead, unregistered code.
@app.middleware("http")
async def sanitize_input_middleware(request: Request, call_next):
    """Reject requests whose query params, headers, or body contain code-injection patterns."""

    for value in request.query_params.values():
        if _contains_dangerous_pattern(value):
            return JSONResponse(
                status_code=400,
                content={"detail": "Request contains disallowed content"},
            )

    for key, value in request.headers.items():
        if key.lower() in _SKIP_HEADERS:
            continue
        if _contains_dangerous_pattern(value):
            return JSONResponse(
                status_code=400,
                content={"detail": "Request contains disallowed content"},
            )

    # 3. Request body — every text-based content type (JSON, form-urlencoded,
    #    multipart/form-data, text/plain). Multipart uploads carry attacker-
    #    controlled file bytes, so they must be scanned too; _is_text_content_type
    #    enumerates the set.
    if request.method in ("POST", "PUT", "PATCH"):
        content_type = request.headers.get("content-type", "")
        if _is_text_content_type(content_type):
            # Reject an oversized body by its declared Content-Length BEFORE
            # reading it — cheapest path, and stops a huge upload before it's
            # even buffered.
            if _body_too_large(request):
                return JSONResponse(
                    status_code=413,
                    content={"detail": "Request body too large"},
                )
            try:
                from urllib.parse import unquote_plus
                body_bytes = await request.body()
                # Belt-and-suspenders: a missing/lying Content-Length means we
                # only learn the true size here — reject before any regex pass.
                if _body_too_large(request, len(body_bytes)):
                    return JSONResponse(
                        status_code=413,
                        content={"detail": "Request body too large"},
                    )
                raw_text = body_bytes.decode("utf-8", errors="ignore")
                body_text = unquote_plus(raw_text) if "form-urlencoded" in content_type else raw_text
                if _contains_dangerous_pattern(body_text):
                    return JSONResponse(
                        status_code=400,
                        content={"detail": "Request contains disallowed content"},
                    )
                # Re-inject the consumed body so downstream handlers can read it.
                # We rebuild the ASGI receive channel rather than only setting
                # request._body: under an ASGI test transport, the downstream
                # route constructs its OWN Request from the scope and calls
                # receive() to read the body — if the stream is drained and we
                # only stashed _body on THIS Request instance, that receive()
                # blocks forever (observed as a selector.select hang on Linux
                # CI). A receive() that replays the cached bytes is what every
                # consumer (Starlette Request.body, Pydantic binding) honours.
                request._body = body_bytes

                async def _replay_receive() -> dict:
                    return {
                        "type": "http.request",
                        "body": body_bytes,
                        "more_body": False,
                    }
                request = Request(request.scope, receive=_replay_receive)
            except Exception:
                pass

    return await call_next(request)


@app.middleware("http")
async def strict_cors_middleware(request: Request, call_next):
    """Exact-match CORS guard layered on top of CORSMiddleware (SEC-004).

    CORSMiddleware already declines to echo an origin that isn't on the
    allowlist, but for a *preflight* (OPTIONS + Access-Control-Request-Method)
    from a spoofed origin it still returns 200 with no CORS headers. That is
    indistinguishable to a browser from a transient error and leaks no signal
    to defenders. We make the rejection explicit: a preflight from an origin
    that is not a byte-for-byte allowlist member gets a hard 403.

    Non-preflight requests pass straight through — CORSMiddleware owns the
    Access-Control-Allow-Origin reflection for those, and it only reflects
    allowlisted origins, so a spoofed origin is never echoed.
    """
    origin = request.headers.get("origin")
    is_preflight = (
        request.method == "OPTIONS"
        and request.headers.get("access-control-request-method") is not None
    )
    if origin and is_preflight and not _is_origin_allowed(origin):
        return JSONResponse(
            status_code=403,
            content={"detail": "Origin not allowed"},
        )
    return await call_next(request)


# HSTS is opt-in (ENABLE_HSTS=true) because the header must only be sent when the
# site is genuinely served over TLS — emitting it on a plain-http dev origin would
# wrongly pin the browser to https for a year. In production behind a TLS proxy
# (Azure Container Apps terminates TLS), set ENABLE_HSTS=true. The other headers
# below are always-safe and sent unconditionally.
_HSTS_ENABLED = os.getenv("ENABLE_HSTS", "false").strip().lower() in ("1", "true", "yes")


@app.middleware("http")
async def security_headers_middleware(request: Request, call_next):
    """Attach hardening response headers. HSTS (gated on ENABLE_HSTS so we never
    pin a plain-http dev origin to https); plus always-safe headers that cost
    nothing and close common low-severity findings (clickjacking, MIME-sniffing,
    referrer leakage)."""
    response = await call_next(request)
    if _HSTS_ENABLED:
        response.headers.setdefault(
            "Strict-Transport-Security",
            "max-age=31536000; includeSubDomains",
        )
    response.headers.setdefault("X-Content-Type-Options", "nosniff")
    response.headers.setdefault("X-Frame-Options", "DENY")
    response.headers.setdefault("Referrer-Policy", "no-referrer")
    return response


# Kill-switch for the limiter. Defaults ON. The test suite sets this to "0" so
# the many TestClient calls (all sharing the loopback IP) don't accumulate into
# a spurious 429 — limiter BEHAVIOUR is covered directly in
# test_rate_limiter_hardening.py, which flips it back on for its assertions.
def _rate_limit_enabled() -> bool:
    return os.getenv("SHIPMATE_RATE_LIMIT", "1").strip().lower() not in ("0", "false", "no")


@app.middleware("http")
async def rate_limit_middleware(request: Request, call_next):
    """Per-IP token-bucket rate limiting for the sensitive endpoints (auth
    callback, analyze, webhook). Registered LAST so it's the OUTERMOST layer —
    an over-limit request is rejected with 429 + Retry-After before any body
    sanitization or routing work happens. The IP is resolved via _get_client_ip
    (socket peer unless TRUST_PROXY_HEADERS), so the key can't be spoofed."""
    if _rate_limit_enabled():
        limiter, message = _limiter_for_path(request.url.path)
        if limiter is not None and not limiter.is_allowed(_get_client_ip(request)):
            return JSONResponse(
                status_code=429,
                content={"detail": f"Rate limit exceeded. {message}"},
                # Standards-compliant backoff hint so clients don't retry-storm.
                headers={"Retry-After": str(limiter.retry_after_seconds())},
            )
    return await call_next(request)


app.include_router(auth_router, prefix="/api")
app.include_router(analysis_router, prefix="/api")
app.include_router(actuate_router, prefix="/api")
app.include_router(watcher_router, prefix="/api")
app.include_router(branches_router, prefix="/api")
app.include_router(findings_router, prefix="/api")
app.include_router(auto_fix_router, prefix="/api")
app.include_router(build_router, prefix="/api")
app.include_router(metrics_router, prefix="/api")
app.include_router(research_router, prefix="/api")


@app.get("/")
async def root():
    return {
        "service": "ShipMate AI",
        "version": "2.0.0",
        "status": "operational",
        "docs": "/docs",
        "agents": ["RepoLens", "PlanForge", "GuardRail", "TestPilot"],
    }


@app.get("/health")
async def health():
    return {"status": "healthy", "agents": 4}


def _verify_github_webhook_signature(body: bytes, signature_header: str) -> bool:
    """Verify a GitHub webhook's X-Hub-Signature-256 header (HMAC-SHA256).

    GitHub signs the raw request body with the shared secret (configured in
    the repo's webhook settings) and sends ``sha256=<hexdigest>``. We recompute
    the HMAC over the exact bytes we received and compare in constant time.

    Returns False when:
      - GITHUB_WEBHOOK_SECRET is unset (fail closed — never accept unsigned
        webhooks in that case),
      - the header is missing or malformed,
      - the digests don't match.
    """
    secret = os.getenv("GITHUB_WEBHOOK_SECRET", "")
    if not secret:
        # Fail closed: without a configured secret we cannot authenticate the
        # sender, so we reject rather than process attacker-controlled payloads.
        logger.warning("GITHUB_WEBHOOK_SECRET not set — rejecting webhook")
        return False
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    expected = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    provided = signature_header.split("=", 1)[1]
    return hmac.compare_digest(expected, provided)


@app.post("/webhooks/github")
async def github_webhook(request: Request):
    """Handle GitHub webhook events for push and pull_request.

    Validates webhook signature, extracts repository and branch info,
    and triggers automatic analysis.
    """
    # Reject an oversized webhook body before buffering/HMAC — a webhook payload
    # is small; an outsized one is abuse.
    if _body_too_large(request):
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )
    # Get raw body for signature verification
    body_bytes = await request.body()
    if _body_too_large(request, len(body_bytes)):
        return JSONResponse(
            status_code=413,
            content={"detail": "Request body too large"},
        )

    # Verify webhook signature
    signature = request.headers.get("x-hub-signature-256", "")
    if not _verify_github_webhook_signature(body_bytes, signature):
        return JSONResponse(
            status_code=401,
            content={"detail": "Invalid webhook signature"},
        )

    try:
        payload = json.loads(body_bytes.decode("utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JSONResponse(
            status_code=400,
            content={"detail": "Invalid JSON payload"},
        )

    event_type = request.headers.get("x-github-event", "")

    if event_type == "push":
        repo_name = payload.get("repository", {}).get("full_name")
        branch = payload.get("ref", "").split("/")[-1]

        if not repo_name or not branch:
            return JSONResponse(
                status_code=400,
                content={"detail": "Missing repository or branch info"},
            )

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "message": f"Analysis queued for {repo_name}:{branch}",
                "event": "push",
            },
        )

    elif event_type == "pull_request":
        repo_name = payload.get("repository", {}).get("full_name")
        pr_number = payload.get("pull_request", {}).get("number")
        action = payload.get("action")

        if not repo_name or not pr_number:
            return JSONResponse(
                status_code=400,
                content={"detail": "Missing repository or PR info"},
            )

        if action not in ["opened", "synchronize", "reopened"]:
            return JSONResponse(
                status_code=202,
                content={
                    "status": "ignored",
                    "message": f"PR action '{action}' does not trigger analysis",
                    "event": "pull_request",
                },
            )

        return JSONResponse(
            status_code=202,
            content={
                "status": "accepted",
                "message": f"Analysis queued for {repo_name} PR #{pr_number}",
                "event": "pull_request",
            },
        )

    else:
        return JSONResponse(
            status_code=202,
            content={
                "status": "ignored",
                "message": f"Event type '{event_type}' is not processed",
            },
        )


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
