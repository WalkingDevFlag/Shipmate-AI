from dotenv import load_dotenv
load_dotenv()

import os
import re
import json
import time
import hmac
import hashlib
import logging
import unicodedata
from collections import OrderedDict
from io import BytesIO
from typing import Any, Callable
from urllib.parse import urlparse
import threading

logger = logging.getLogger("shipmate.main")

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
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

# ---------------------------------------------------------------------------
# Rate Limiting: Token Bucket Implementation
# ---------------------------------------------------------------------------

class _TokenBucket:
    """Simple token bucket for rate limiting.
    
    Tokens are refilled at a constant rate. When a request arrives,
    we check if a token is available; if so, consume it and allow the request.
    Otherwise, reject the request.
    """
    
    def __init__(self, capacity: int, refill_rate: float):
        """Initialize token bucket.
        
        Args:
            capacity: Maximum number of tokens (burst size).
            refill_rate: Tokens per second to refill.
        """
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.tokens = float(capacity)
        self.last_refill = time.time()
        self._lock = threading.Lock()
    
    def allow_request(self) -> bool:
        """Check if a request is allowed and consume a token if so.
        
        Returns True if a token was available, False otherwise.
        """
        with self._lock:
            now = time.time()
            elapsed = now - self.last_refill
            
            # Refill tokens based on elapsed time
            self.tokens = min(
                self.capacity,
                self.tokens + elapsed * self.refill_rate
            )
            self.last_refill = now
            
            # Try to consume a token
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False


class _RateLimiter:
    """Per-IP rate limiter using token buckets.

    Bucket eviction (was an unbounded memory leak): every distinct client IP
    created a bucket that was NEVER removed, so steady traffic 1 or a flood of
    spoofed X-Forwarded-For values 1 grew `buckets` without bound. We now (a)
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
        # at the front 1 stop at the first still-fresh entry.
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
        return bucket.allow_request()


# Rate limiters for sensitive endpoints
# Auth callback: 10 requests per minute per IP (burst of 2)
_auth_limiter = _RateLimiter(capacity=2, refill_rate=10.0 / 60.0)

# Analysis endpoint: 30 requests per minute per IP (burst of 5)
_analysis_limiter = _RateLimiter(capacity=5, refill_rate=30.0 / 60.0)

# Webhook endpoint: 60 requests per minute per IP (burst of 10)
_webhook_limiter = _RateLimiter(capacity=10, refill_rate=60.0 / 60.0)


# X-Forwarded-For is CLIENT-CONTROLLED and must only be trusted when the
# request actually arrives through a known reverse proxy that appends it.
# Trusting it unconditionally (the old behavior) let anyone spoof their per-IP
# rate-limit key 1 send a random XFF per request and every request looks like a
# brand-new IP, defeating the limiter AND inflating the bucket map. We only honor
# XFF when TRUST_PROXY_HEADERS is enabled (set it true behind Azure Container
# Apps / any trusted ingress that sets the header); otherwise we use the real
# socket peer address, which a client cannot forge.
_TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "false").strip().lower() in ("1", "true", "yes")


def _get_client_ip(request: Request) -> str:
    """Resolve the client IP for rate-limiting.

    When TRUST_PROXY_HEADERS is set (deployed behind a trusted proxy that
    populates X-Forwarded-For), take the left-most XFF entry. Otherwise 1 and
    by default 1 use the unforgeable socket peer address. Never trust an
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


async def rate_limit_auth_middleware(request: Request, call_next: Callable):
    """Rate limit the /auth/callback endpoint."""
    if request.url.path == "/api/auth/github/callback":
        client_ip = _get_client_ip(request)
        if not _auth_limiter.is_allowed(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Too many authentication attempts."},
            )
    return await call_next(request)


async def rate_limit_analysis_middleware(request: Request, call_next: Callable):
    """Rate limit the /analysis endpoint."""
    if request.url.path.startswith("/api/analysis"):
        client_ip = _get_client_ip(request)
        if not _analysis_limiter.is_allowed(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Too many analysis requests."},
            )
    return await call_next(request)


async def rate_limit_webhook_middleware(request: Request, call_next: Callable):
    """Rate limit the /webhooks/github endpoint."""
    if request.url.path == "/webhooks/github":
        client_ip = _get_client_ip(request)
        if not _webhook_limiter.is_allowed(client_ip):
            return JSONResponse(
                status_code=429,
                content={"detail": "Rate limit exceeded. Too many webhook requests."},
            )
    return await call_next(request)


# ---------------------------------------------------------------------------
# Startup: Enforce HTTPS for all configured service endpoint URLs
# ---------------------------------------------------------------------------

# Environment variables that may carry service endpoint URLs and must use HTTPS
# when set to a non-localhost/non-loopback value.
_ENDPOINT_ENV_VARS = [
    "BEDROCK_ENDPOINT",
    "API_BASE_URL",
    "INTERNAL_API_URL",
    "GITHUB_API_URL",
    "OPENAI_API_BASE",
    "ANTHROPIC_API_URL",
]

# Hostnames that are considered local-only and are exempt from the HTTPS
# requirement (plain HTTP is acceptable for loopback-only traffic).
_LOCAL_HOSTS = frozenset([
    "localhost",
    "127.0.0.1",
    "::1",
    "0.0.0.0",
])


def _validate_endpoint_urls() -> None:
    """Raise ValueError if any configured endpoint URL uses plain HTTP with a
    non-local host.  Called at module import time so the application refuses to
    start with an insecure configuration.
    """
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
# scanner) used to sit here and silently SHADOWED those real definitions 1
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
# The wildcard "*" is intentionally NOT supported here because
# allow_credentials=True is incompatible with "*" and would expose
# authenticated endpoints to any third-party site.
# ---------------------------------------------------------------------------

def _validate_origin(origin: str) -> str:
    """Validate a single CORS origin string.

    Rules:
    - Must not be '*' or contain any wildcard character ('*').
    - Must parse to a URL whose scheme is 'http' or 'https'.
    - Must have a non-empty netloc (host).

    Returns the origin unchanged if valid, raises ValueError otherwise.
    """
    if "*" in origin:
        raise ValueError(
            f"CORS origin '{origin}' contains a wildcard character, which is not "
            "allowed when allow_credentials=True. Set ALLOWED_ORIGINS to explicit "
            "origins only."
        )
    parsed = urlparse(origin)
    if parsed.scheme not in ("http", "https"):
        raise ValueError(
            f"CORS origin '{origin}' has an invalid scheme '{parsed.scheme}'. "
            "Only 'http' and 'https' schemes are permitted."
        )
    if not parsed.netloc:
        raise ValueError(
            f"CORS origin '{origin}' has an empty host/netloc. "
            "Each origin must be a fully-qualified URL such as 'https://example.com'."
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

    Deliberately strict 1 no prefix, suffix, or subdomain matching. This is
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
# Input sanitization 1 block code injection patterns in query params / headers
# / request body before they reach any route handler.
# ---------------------------------------------------------------------------
_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"eval\s*\(",            re.IGNORECASE),
    re.compile(r"exec\s*\(",            re.IGNORECASE),
    re.compile(r"__import__\s*\(",      re.IGNORECASE),
    re.compile(r"__builtins__",         re.IGNORECASE),
    re.compile(r"__globals__",          re.IGNORECASE),
    re.compile(r"__locals__",           re.IGNORECASE),
    re.compile(r"compile\s*\(",         re.IGNORECASE),
    re.compile(r"importlib\.import_module", re.IGNORECASE),
    re.compile(r"subprocess\.",         re.IGNORECASE),
    re.compile(r"os\.system\s*\(",      re.IGNORECASE),
    re.compile(r"os\.popen\s*\(",       re.IGNORECASE),
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


# ... rest of file unchanged ...
