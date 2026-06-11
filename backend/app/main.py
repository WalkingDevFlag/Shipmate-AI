import os
import re
import threading
import time
from collections import OrderedDict
from typing import Optional
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.types import ASGIApp

from app.api.routes.actuate import router as actuate_router
from app.api.routes.analysis import router as analysis_router
from app.api.routes.auth import router as auth_router
from app.api.routes.auto_fix import router as auto_fix_router
from app.api.routes.branches import router as branches_router
from app.api.routes.build import router as build_router
from app.api.routes.findings import router as findings_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.watcher import router as watcher_router

ALLOWED_ORIGINS = [o for o in os.getenv("CORS_ALLOWED_ORIGINS", "*").split(",") if o]
_HSTS_ENABLED = os.getenv("ENABLE_HSTS", "0") == "1"
_TRUST_PROXY_HEADERS = os.getenv("TRUST_PROXY_HEADERS", "0") == "1"

# --- Security Headers Middleware ---
class SecurityHeadersMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp):
        super().__init__(app)

    async def dispatch(self, request: Request, call_next):
        response = await call_next(request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "same-origin"
        response.headers["X-XSS-Protection"] = "1; mode=block"
        if _HSTS_ENABLED:
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

# --- Input Sanitization ---
_DANGEROUS_PATTERNS = [
    re.compile(r"\\beval\\s*\\(", re.IGNORECASE),
    re.compile(r"\\bexec\\s*\\(", re.IGNORECASE),
    re.compile(r"__import__\\s*\\(", re.IGNORECASE),
    re.compile(r"os\\.system\\s*\\(", re.IGNORECASE),
    re.compile(r"subprocess\\.Popen\\s*\\(", re.IGNORECASE),
    re.compile(r"subprocess\\.call\\s*\\(", re.IGNORECASE),
    re.compile(r"subprocess\\.run\\s*\\(", re.IGNORECASE),
]

def _normalize_for_scanning(s: str) -> str:
    # Remove url encoding, collapse whitespace, etc.
    try:
        import urllib.parse
        s = urllib.parse.unquote_plus(s)
        s = urllib.parse.unquote(s)
    except Exception:
        pass
    return re.sub(r"\\s+", " ", s)

def _contains_dangerous_pattern(s: str) -> bool:
    s = _normalize_for_scanning(s)
    for pat in _DANGEROUS_PATTERNS:
        if pat.search(s):
            return True
    return False

class InputSanitizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.method in ("POST", "PUT", "PATCH"):
            body = await request.body()
            if _contains_dangerous_pattern(body.decode(errors="ignore")):
                return JSONResponse(status_code=400, content={"detail": "Dangerous input detected."})
        return await call_next(request)

# --- Token Bucket Rate Limiter ---
class _TokenBucket:
    def __init__(self, capacity: int, refill_rate: float):
        self.capacity = capacity
        self.tokens = capacity
        self.refill_rate = refill_rate
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, tokens: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            refill = elapsed * self.refill_rate
            if refill > 0:
                self.tokens = min(self.capacity, self.tokens + refill)
                self.last_refill = now
            if self.tokens >= tokens:
                self.tokens -= tokens
                return True
            return False

class _RateLimiter:
    def __init__(self, capacity: int, refill_rate: float, max_entries: int = 1000):
        self.capacity = capacity
        self.refill_rate = refill_rate
        self.max_entries = max_entries
        self.buckets = OrderedDict()
        self._lock = threading.Lock()

    def get_bucket(self, key: str) -> _TokenBucket:
        with self._lock:
            bucket = self.buckets.get(key)
            if bucket is None:
                if len(self.buckets) >= self.max_entries:
                    self.buckets.popitem(last=False)
                bucket = _TokenBucket(self.capacity, self.refill_rate)
                self.buckets[key] = bucket
            else:
                # Move to end to mark as recently used
                self.buckets.move_to_end(key)
            return bucket

# --- FastAPI App ---
app = FastAPI()

app.add_middleware(SecurityHeadersMiddleware)
app.add_middleware(InputSanitizationMiddleware)
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS if ALLOWED_ORIGINS != ["*"] else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(actuate_router, prefix="/api/actuate")
app.include_router(analysis_router, prefix="/api/analyze")
app.include_router(auth_router, prefix="/api/auth")
app.include_router(auto_fix_router, prefix="/api/auto-fix")
app.include_router(branches_router, prefix="/api/branches")
app.include_router(build_router, prefix="/api/build")
app.include_router(findings_router, prefix="/api/findings")
app.include_router(metrics_router, prefix="/api/metrics")
app.include_router(watcher_router, prefix="/api/watcher")
