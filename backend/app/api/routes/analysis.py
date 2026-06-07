from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.schemas.api_schemas import AnalyzeRequest, AnalyzeResponse
from app.services.repo_analysis_service import RepoAnalysisService
from app.orchestrator.shipmate_orchestrator import ShipMateOrchestrator

import httpx
import json

router = APIRouter(tags=["analysis"])

_orchestrator = ShipMateOrchestrator()

# Permissions that indicate write access to a repository.
_WRITE_PERMISSIONS = {"admin", "maintain", "write", "push"}


async def _verify_repo_write_access(token: str, owner: str, repo: str) -> None:
    """
    Verify that the supplied GitHub token grants write access to owner/repo.

    Calls the GitHub Collaborator Permission API with the authenticated user's
    token.  Raises HTTP 403 if the token lacks write access, or HTTP 404 if
    the repository does not exist / is not visible to the token.

    Raises HTTPException on any access-control or network failure.
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    url = f"https://api.github.com/repos/{owner}/{repo}"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.get(url, headers=headers)
    except Exception as exc:
        raise HTTPException(
            status_code=502,
            detail=f"Unable to reach GitHub API to verify repository access: {exc}",
        )

    if response.status_code == 404:
        raise HTTPException(
            status_code=404,
            detail=f"Repository '{owner}/{repo}' not found or not accessible with the provided token.",
        )

    if response.status_code == 401:
        raise HTTPException(
            status_code=401,
            detail="GitHub token is invalid or expired.",
        )

    if response.status_code not in (200, 301):
        raise HTTPException(
            status_code=502,
            detail=f"GitHub API returned unexpected status {response.status_code} while checking repository access.",
        )

    repo_data = response.json()
    # The /repos/{owner}/{repo} response includes a `permissions` object when
    # the token has any access.  If the object is absent the token is read-only
    # (e.g. a public-repo token with no write scope).
    permissions: dict = repo_data.get("permissions", {})
    has_write = (
        permissions.get("admin") is True
        or permissions.get("maintain") is True
        or permissions.get("push") is True
    )

    if not has_write:
        raise HTTPException(
            status_code=403,
            detail=(
                f"The authenticated token does not have write access to '{owner}/{repo}'. "
                "Only repositories where you have push, maintain, or admin permission "
                "may be analysed."
            ),
        )


async def _build_context_or_502(request: AnalyzeRequest):
    """Shared prelude for both analyze endpoints: validate, authorize, fetch
    repo context. Raises HTTPException on any failure."""
    if not request.owner or not request.repo:
        raise HTTPException(status_code=400, detail="owner and repo are required.")

    # Authorization check: token must have write access to the target repo.
    await _verify_repo_write_access(
        token=request.access_token, owner=request.owner, repo=request.repo,
    )

    try:
        return await RepoAnalysisService.build_context(
            token=request.access_token,
            owner=request.owner,
            repo=request.repo,
            branch=request.branch,
            pr_number=request.pr_number,
            feature_context=request.feature_context or "",
        )
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch repository data from GitHub: {str(e)}",
        )


@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    """
    Run the ShipMate 4-agent analysis pipeline and return the full report.

    Non-streaming JSON contract (status + ShipMateReport). This is the stable
    API the web UI's "Run Analysis", the autonomous loop, and the CLI loop all
    consume. For incremental per-agent updates use POST /api/analyze/stream.

    Flow: RepoLens → PlanForge / GuardRail / TestPilot → Score → report.
    """
    repo_context = await _build_context_or_502(request)
    try:
        report = _orchestrator.run(repo_context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis pipeline failed: {str(e)}")
    return AnalyzeResponse(status="complete", report=report)


async def _stream_analysis(repo_context):
    """Yield one SSE `data:` frame per agent result, then a `[DONE]` sentinel.
    The final frame carries {"agent": "report", "output": <full report>}."""
    async for agent_result in _orchestrator.run_stream(repo_context):
        yield f"data: {json.dumps(agent_result)}\n\n"
    yield "data: [DONE]\n\n"


@router.post("/analyze/stream")
async def analyze_stream(request: AnalyzeRequest):
    """Streaming (SSE) variant of /analyze: emits each agent's result as it
    completes, then a final assembled-report frame and a [DONE] sentinel.
    Same auth + context-building as /analyze."""
    repo_context = await _build_context_or_502(request)
    return StreamingResponse(
        _stream_analysis(repo_context),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )
