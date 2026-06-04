from dotenv import load_dotenv
load_dotenv()

import os
import re

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
import uvicorn

from app.api.routes.auth import router as auth_router
from app.api.routes.analysis import router as analysis_router
from app.db.database import init_db

# ---------------------------------------------------------------------------
# CORS origin allowlist
# In production set ALLOWED_ORIGINS to a comma-separated list of origins, e.g.:
#   ALLOWED_ORIGINS=https://app.shipmate.ai
# The wildcard "*" is intentionally NOT supported here because
# allow_credentials=True is incompatible with "*" and would expose
# authenticated endpoints to any third-party site.
# ---------------------------------------------------------------------------
_raw_origins = os.getenv(
    "ALLOWED_ORIGINS",
    "https://localhost:5173,https://localhost:5174,https://localhost:3000,https://127.0.0.1:5173",
)
ALLOWED_ORIGINS: list[str] = [
    origin.strip() for origin in _raw_origins.split(",") if origin.strip()
]

# ---------------------------------------------------------------------------
# Dangerous pattern blocklist for input sanitization.
# These patterns cover the most common dynamic-execution vectors that could
# be injected via user-controlled strings (prompt inputs, API parameters).
# ---------------------------------------------------------------------------
_DANGEROUS_PATTERNS: list[re.Pattern] = [
    re.compile(r"\beval\s*\(", re.IGNORECASE),
    re.compile(r"\bexec\s*\(", re.IGNORECASE),
    re.compile(r"__import__\s*\(", re.IGNORECASE),
    re.compile(r"__builtins__", re.IGNORECASE),
    re.compile(r"__globals__", re.IGNORECASE),
    re.compile(r"__locals__", re.IGNORECASE),
    re.compile(r"compile\s*\(", re.IGNORECASE),
    re.compile(r"importlib\.import_module\s*\(", re.IGNORECASE),
    re.compile(r"subprocess\s*\.", re.IGNORECASE),
    re.compile(r"os\.system\s*\(", re.IGNORECASE),
    re.compile(r"os\.popen\s*\(", re.IGNORECASE),
]


def _contains_dangerous_pattern(value: str) -> bool:
    """Return True if *value* matches any known dangerous execution pattern."""
    for pattern in _DANGEROUS_PATTERNS:
        if pattern.search(value):
            return True
    return False


app = FastAPI(
    title="ShipMate AI",
    description="AI-native multi-agent release readiness platform",
    version="2.0.0",
    docs_url="/docs",
    redoc_url="/redoc",
)


# ---------------------------------------------------------------------------
# HTTPS enforcement middleware
# In production (ENFORCE_HTTPS=true) any plain HTTP request is redirected
# to its HTTPS equivalent with a 301 permanent redirect.
# Disabled by default so local development without TLS is unaffected.
# ---------------------------------------------------------------------------
_ENFORCE_HTTPS = os.getenv("ENFORCE_HTTPS", "false").lower() == "true"


@app.middleware("http")
async def https_redirect_middleware(request: Request, call_next):
    if _ENFORCE_HTTPS and request.url.scheme == "http":
        https_url = request.url.replace(scheme="https")
        return RedirectResponse(url=str(https_url), status_code=301)
    return await call_next(request)


# ---------------------------------------------------------------------------
# Input sanitization middleware
# Runs before every request and rejects any payload that contains patterns
# associated with dynamic code execution (eval, exec, __import__, etc.).
# ---------------------------------------------------------------------------
@app.middleware("http")
async def sanitize_input_middleware(request: Request, call_next):
    # --- Check query parameters ---
    for key, value in request.query_params.items():
        if _contains_dangerous_pattern(key) or _contains_dangerous_pattern(value):
            return JSONResponse(
                status_code=400,
                content={"detail": "Request contains disallowed content."},
            )

    # --- Check selected headers (User-Agent, Referer, custom X- headers) ---
    _checked_headers = {
        k: v
        for k, v in request.headers.items()
        if k.lower() not in ("authorization", "cookie")
    }
    for key, value in _checked_headers.items():
        if _contains_dangerous_pattern(value):
            return JSONResponse(
                status_code=400,
                content={"detail": "Request contains disallowed content."},
            )

    # --- Check request body for JSON/text content types ---
    content_type = request.headers.get("content-type", "")
    if any(ct in content_type for ct in ("application/json", "text/", "application/x-www-form-urlencoded")):
        try:
            body_bytes = await request.body()
            body_text = body_bytes.decode("utf-8", errors="replace")
            if _contains_dangerous_pattern(body_text):
                return JSONResponse(
                    status_code=400,
                    content={"detail": "Request contains disallowed content."},
                )
        except Exception:
            # If we cannot read the body, let the route handler deal with it.
            pass

    return await call_next(request)


app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.include_router(auth_router, prefix="/api")
app.include_router(analysis_router, prefix="/api")


@app.on_event("startup")
async def startup_event():
    """Initialize database on application startup."""
    init_db()


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


@app.get("/github-callback.html", response_class=HTMLResponse)
async def github_callback_html():
    return HTMLResponse(content="""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <title>ShipMate AI \u2013 GitHub Auth</title>
  <style>
    *{margin:0;padding:0;box-sizing:border-box}
    body{font-family:system-ui,sans-serif;background:#0f172a;display:flex;align-items:center;
         justify-content:center;min-height:100vh;color:#e2e8f0}
    .box{text-align:center;padding:2rem}
    .spinner{width:48px;height:48px;border:4px solid rgba(99,179,237,.2);
             border-top-color:#60a5fa;border-radius:50%;animation:spin 1s linear infinite;
             margin:0 auto 1.5rem}
    @keyframes spin{to{transform:rotate(360deg)}}
    h1{font-size:1.25rem;margin-bottom:.5rem}
    p{color:#94a3b8;font-size:.875rem}
    .error{color:#fca5a5;margin-top:1rem;padding:.75rem 1rem;
           background:rgba(239,68,68,.1);border-radius:.5rem;font-size:.875rem}
  </style>
</head>
<body>
  <div class="box">
    <div class="spinner"></div>
    <h1>Completing GitHub authorization\u2026</h1>
    <p>This window will close automatically.</p>
    <div id="err"></div>
  </div>
  <script>
    const p = new URLSearchParams(location.search);
    const code  = p.get('code');
    const state = p.get('state');
    const err   = p.get('error');
    if (err) {
      document.getElementById('err').innerHTML =
        '<div class="error">Authorization failed: ' + (p.get('error_description') || err) + '</div>';
      setTimeout(() => window.close(), 3000);
    } else if (code) {
      fetch('/api/auth/github/callback?code=' + encodeURIComponent(code) + '&state=' + encodeURIComponent(state || ''))
        .then(r => r.json())
        .then(d => {
          if (d.success) {
            window.opener && window.opener.postMessage(
              {type:'GITHUB_AUTH_SUCCESS', access_token: d.access_token, user: d.user}, '*');
            window.close();
          } else { throw new Error(d.detail || 'Auth failed'); }
        })
        .catch(e => {
          document.getElementById('err').innerHTML =
            '<div class="error">' + e.message + '</div>';
          setTimeout(() => window.close(), 3000);
        });
    }
  </script>
</body>
</html>""")


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
