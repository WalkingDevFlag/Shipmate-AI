from dotenv import load_dotenv
load_dotenv()  # Load .env file before anything else

from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
import uvicorn
import os

from app.models.schemas import (
    AnalyzeRequest, AnalyzeResponse, RepoSummary,
    GitHubRepository, GitHubBranch
)
from app.services.analyzer import run_full_analysis
from app.services.repo_service import process_repo_zip, get_sample_repo
from app.routes.auth_routes import router as auth_router

app = FastAPI(
    title="ShipMate AI API",
    description="Agentic Engineering Command Center — Production Readiness Analysis",
    version="1.0.0",
    docs_url="/docs",
    redoc_url="/redoc"
)

# ---------------------------------------------------------------------------
# CORS — origins are loaded from the ALLOWED_ORIGINS environment variable.
# Set ALLOWED_ORIGINS to a comma-separated list of trusted domains, e.g.:
#   ALLOWED_ORIGINS=https://shipmate.example.com,https://staging.shipmate.example.com
# Defaults to localhost dev servers when the variable is not set.
# allow_credentials is only enabled when the origin list is explicitly
# restricted (i.e. does not contain a wildcard).
# ---------------------------------------------------------------------------
_raw_origins = os.environ.get(
    "ALLOWED_ORIGINS",
    "http://localhost:5173,http://localhost:5174,http://localhost:3000,"
    "http://127.0.0.1:5173,http://127.0.0.1:5174,http://127.0.0.1:3000",
)
allowed_origins = [origin.strip() for origin in _raw_origins.split(",") if origin.strip()]

# Never allow credentials alongside a wildcard origin.
_has_wildcard = "*" in allowed_origins
if _has_wildcard:
    # Safety net: if someone accidentally sets ALLOWED_ORIGINS=*, strip the
    # wildcard and fall back to an empty list so the server starts safely
    # rather than silently exposing credentialed endpoints to every origin.
    import warnings
    warnings.warn(
        "ALLOWED_ORIGINS contains a wildcard '*'. "
        "Wildcard origins are not permitted with credentialed requests. "
        "Falling back to no allowed origins — set ALLOWED_ORIGINS to an "
        "explicit list of trusted domains.",
        stacklevel=1,
    )
    allowed_origins = []

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,  # safe: wildcard is rejected above
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================================
# Include routers
# ============================================================================
app.include_router(auth_router, prefix="/api")



@app.get("/")
async def root():
    return {
        "service": "ShipMate AI — Agentic Engineering Command Center",
        "version": "1.0.0",
        "status": "operational",
        "docs": "/docs",
        "tagline": "Production Readiness Analysis powered by Azure AI Foundry + Azure OpenAI"
    }


@app.get("/health")
async def health_check():
    return {
        "status": "healthy",
        "agents": [
            "planner",
            "repo_analyst",
            "test_generator",
            "security_guard",
            "delivery_manager"
        ],
        "message": "All systems operational"
    }


# ============================================================================
# GitHub Callback HTML - Serves the OAuth callback page
# ============================================================================

@app.get("/github-callback.html")
async def github_callback_html():
    """Serve GitHub OAuth callback HTML page"""
    from fastapi.responses import HTMLResponse
    html_content = """<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>ShipMate AI - GitHub Authorization</title>
    <style>
        * {
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Roboto', 'Oxygen',
                'Ubuntu', 'Cantarell', 'Fira Sans', 'Droid Sans', 'Helvetica Neue', sans-serif;
            background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%);
            display: flex;
            align-items: center;
            justify-content: center;
            min-height: 100vh;
            color: #e2e8f0;
        }
        .container {
            text-align: center;
            padding: 2rem;
        }
        .spinner {
            width: 50px;
            height: 50px;
            border: 4px solid rgba(96, 165, 250, 0.2);
            border-top: 4px solid #60a5fa;
            border-radius: 50%;
            animation: spin 1s linear infinite;
            margin: 0 auto 2rem;
        }
        @keyframes spin {
            to { transform: rotate(360deg); }
        }
        h1 {
            font-size: 1.5rem;
            margin-bottom: 0.5rem;
        }
        p {
            color: #94a3b8;
        }
        .error {
            color: #fca5a5;
            margin-top: 1rem;
            padding: 1rem;
            background: rgba(239, 68, 68, 0.1);
            border-radius: 0.5rem;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="spinner"></div>
        <h1>Authorizing with GitHub...</h1>
        <p>Please wait while we complete your authentication.</p>
        <div id="error"></div>
    </div>

    <script>
        // Get OAuth code and state from URL
        const params = new URLSearchParams(window.location.search);
        const code = params.get('code');
        const state = params.get('state');
        const error = params.get('error');
        const errorDesc = params.get('error_description');

        if (error) {
            document.getElementById('error').innerHTML = 
                `<div class="error"><strong>Authorization Failed:</strong> ${errorDesc || error}</div>`;
            setTimeout(() => {
                window.close();
            }, 3000);
        } else if (code) {
            // Exchange code for access token
            fetch('/api/github/callback?code=' + encodeURIComponent(code) + '&state=' + encodeURIComponent(state))
                .then(response => response.json())
                .then(data => {
                    if (data.success) {
                        // Post message to parent window
                        window.opener.postMessage({
                            type: 'GITHUB_AUTH_SUCCESS',
                            access_token: data.access_token,
                            user: data.user
                        }, '*');
                        window.close();
                    } else {
                        throw new Error(data.detail || 'Authentication failed');
                    }
                })
                .catch(err => {
                    document.getElementById('error').innerHTML = 
                        `<div class="error"><strong>Authorization Failed:</strong> ${err.message}</div>`;
                    setTimeout(() => {
                        window.close();
                    }, 3000);
                });
        }
    </script>
</body>
</html>"""
    return HTMLResponse(content=html_content)


# ============================================================================
# Analysis Endpoints
# ============================================================================

@app.post("/api/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    """
    Run the full 5-agent production analysis swarm for a GitHub repository.
    
    Agents:
    1. Planner Agent — Converts feature into engineering tasks
    2. Repo Analyst Agent — Maps impact across the codebase
    3. Test Architect Agent — Creates comprehensive test strategy
    4. Security Guard Agent — Identifies security vulnerabilities
    5. Delivery Manager Agent — Produces release-ready sprint plan
    
    Returns: Production Readiness Score (0-100) + detailed analysis
    """
    if not request.feature_request or len(request.feature_request.strip()) < 10:
        raise HTTPException(
            status_code=400,
            detail="Feature request must be at least 10 characters long."
        )

    try:
        # Run the analysis (repo_details optional - agents use feature_request context)
        result = run_full_analysis(request)
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis failed: {str(e)}")


@app.post("/api/upload-repo", response_model=RepoSummary)
async def upload_repo(file: UploadFile = File(...)):
    """
    Upload a ZIP file containing a repository.
    
    [Legacy endpoint — deprecated in favor of GitHub integration]
    
    Extracts file tree, detects tech stack, and reads key files
    (README, package.json, requirements.txt, etc.)
    """
    if not file.filename:
        raise HTTPException(status_code=400, detail="No file provided.")

    if not file.filename.endswith('.zip'):
        raise HTTPException(
            status_code=400,
            detail="Only ZIP files are supported. Please compress your repository as a ZIP file."
        )

    # 50MB limit
    MAX_SIZE = 50 * 1024 * 1024
    content = await file.read()

    if len(content) > MAX_SIZE:
        raise HTTPException(
            status_code=413,
            detail="File too large. Maximum size is 50MB."
        )

    try:
        summary = await process_repo_zip(content)
        return summary
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Failed to process repository: {str(e)}")


@app.get("/api/sample", response_model=RepoSummary)
async def get_sample():
    """
    Return a sample repository context for demo purposes.
    
    Returns a realistic e-commerce TypeScript project summary.
    """
    return get_sample_repo()


# ============================================================================
# Report Export Endpoint
# ============================================================================

@app.post("/api/reports/export")
async def export_report(analysis_id: str = Query(...)):
    """
    Generate an exportable delivery report.
    
    Later: Connect to Azure Blob Storage for report storage.
    For now: Return report metadata and download URL.
    """
    return {
        "status": "ready",
        "analysis_id": analysis_id,
        "format": "markdown",
        "download_url": f"/api/reports/{analysis_id}/download",
        "expires_in_hours": 24,
        "message": "Report ready for download"
    }


@app.get("/api/reports/{analysis_id}/download")
async def download_report(analysis_id: str):
    """
    Download an exported delivery report.
    
    Later: Stream from Azure Blob Storage.
    """
    return JSONResponse(
        content={
            "message": "Report download feature coming soon",
            "analysis_id": analysis_id
        }
    )


if __name__ == "__main__":
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
