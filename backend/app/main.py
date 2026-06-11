import os
import re
import threading
import time
from typing import Optional

from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.middleware.base import RequestResponseEndpoint
from starlette.types import ASGIApp

from app.api.routes.actuate import router as actuate_router
from app.api.routes.analysis import router as analysis_router
from app.api.routes.auto_fix import router as auto_fix_router
from app.api.routes.auth import router as auth_router
from app.api.routes.branches import router as branches_router
from app.api.routes.build import router as build_router
from app.api.routes.findings import router as findings_router
from app.api.routes.metrics import router as metrics_router
from app.api.routes.watcher import router as watcher_router

_HSTS_ENABLED = os.environ.get("SHIPMATE_HSTS_ENABLED", "0") == "1"

# --- Dangerous pattern scanning (prod hardening tests expect these exact names) ---
def _normalize_for_scanning(s: str) -> str:
    # Lowercase, collapse whitespace, decode url-encoding up to 2x
    import urllib.parse
    s = s.lower()
    for _ in range(2):
        s = urllib.parse.unquote(s)
    s = re.sub(r"\s+", " ", s)
    return s

def _contains_dangerous_pattern(s: str) -> bool:
    s = _normalize_for_scanning(s)
    # Look for eval, exec, __import__, os.system, subprocess, pickle, yaml.load, etc.
    patterns = [
        r"eval\s*\(",
        r"exec\s*\(",
        r"__import__\s*\(",
        r"os\.system\s*\(",
        r"subprocess.*popen",
        r"pickle\.loads",
        r"yaml\.load",
        r"marshal\.loads",
        r"base64\.b64decode",
    ]
    return any(re.search(p, s) for p in patterns)

# --- Token Bucket Rate Limiter (thread-safe) ---
class _TokenBucket:
    def __init__(self, rate: float, capacity: int):
        self.rate = rate
        self.capacity = capacity
        self.tokens = capacity
        self.last_refill = time.monotonic()
        self._lock = threading.Lock()

    def consume(self, amount: int = 1) -> bool:
        with self._lock:
            now = time.monotonic()
            elapsed = now - self.last_refill
            refill = elapsed * self.rate
            if refill > 0:
                self.tokens = min(self.capacity, self.tokens + refill)
                self.last_refill = now
            if self.tokens >= amount:
                self.tokens -= amount
                return True
            return False

# --- Middleware for input sanitization (prod hardening) ---
class InputSanitizationMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        # Only check for POST/PUT/PATCH with JSON or form
        if request.method in ("POST", "PUT", "PATCH"):
            content_type = request.headers.get("content-type", "")
            if "json" in content_type or "form" in content_type:
                try:
                    body = await request.body()
                    if _contains_dangerous_pattern(body.decode(errors="ignore")):
                        return JSONResponse(
                            {"detail": "Dangerous input pattern detected."},
                            status_code=status.HTTP_400_BAD_REQUEST,
                        )
                except Exception:
                    pass
        return await call_next(request)

# --- FastAPI app ---
app = FastAPI(
    docs_url="/docs" if os.environ.get("SHIPMATE_DOCS_ENABLED", "1") == "1" else None,
    redoc_url=None,
    openapi_url="/openapi.json",
)

# --- CORS ---
ALLOWED_ORIGINS = os.environ.get("SHIPMATE_ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# --- Security Headers ---
@app.middleware("http")
async def add_security_headers(request: Request, call_next):
    response = await call_next(request)
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "same-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    if _HSTS_ENABLED:
        response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains; preload"
    return response

# --- Input Sanitization Middleware ---
app.add_middleware(InputSanitizationMiddleware)

# --- Routers ---
app.include_router(actuate_router, prefix="/api/actuate", tags=["actuate"])
app.include_router(analysis_router, prefix="/api/analyze", tags=["analyze"])
app.include_router(auto_fix_router, prefix="/api/auto-fix", tags=["auto-fix"])
app.include_router(auth_router, prefix="/api/auth", tags=["auth"])
app.include_router(branches_router, prefix="/api/branches", tags=["branches"])
app.include_router(build_router, prefix="/api/build", tags=["build"])
app.include_router(findings_router, prefix="/api/findings", tags=["findings"])
app.include_router(metrics_router, prefix="/api/metrics", tags=["metrics"])
app.include_router(watcher_router, prefix="/api/watcher", tags=["watcher"])
