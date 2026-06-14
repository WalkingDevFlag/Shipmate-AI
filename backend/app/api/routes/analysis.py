import asyncio
import json
import logging

from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse
from app.schemas.api_schemas import AnalyzeRequest, AnalyzeResponse
from app.services.repo_analysis_service import RepoAnalysisService
from app.services.repo_index_service import RepoIndexService
from app.services import report_store
from app.orchestrator.shipmate_orchestrator import ShipMateOrchestrator
from app.api.deps import verify_repo_write_access, require_body_credential

logger = logging.getLogger("shipmate.analysis_route")

router = APIRouter(tags=["analysis"])

# No module-global orchestrator: /analyze offloads run() to a worker thread, so
# analyses run concurrently. Each request gets its OWN orchestrator (fresh agent
# instances) so concurrent runs can never share mutable agent state. Agent
# construction is cheap (no I/O in __init__), so this costs ~nothing.

@router.post("/analyze", response_model=AnalyzeResponse)
async def analyze(request: AnalyzeRequest):
    """
    Run the ShipMate 4-agent analysis pipeline on a GitHub repository.

    Flow:
      1. Verify the token has write access to owner/repo
      2. Fetch repo tree + key files from GitHub
      3. RepoLens  → tech stack, architecture, risks
      4. PlanForge → milestones, blockers, delivery plan
      5. GuardRail → security findings, secrets, CORS, auth
      6. TestPilot → coverage gaps, suggested tests
      7. Score → weighted readiness score (0-100)
      8. Return ShipMateReport
    """
    if not request.owner or not request.repo:
        raise HTTPException(status_code=400, detail="owner and repo are required.")

    # Resolve the opaque session id in the body to the real token ONCE, then
    # carry the real token downstream unchanged (vault boundary).
    request.access_token = require_body_credential(request.access_token)

    # --- Authorization check: token must have write access to the target repo ---
    await verify_repo_write_access(request.owner, request.repo, request.access_token)

    try:
        # Single front door — build_context + enrich + RepoLens, cached per
        # (owner, repo, branch, pr_number). Widens the corpus with route/
        # service/agent source so PlanForge discovery (and the capability
        # digest) SEE what already exists — else it re-proposes built features
        # (history endpoint, SSE, etc.) every run. The RepoLens result rides on
        # the context so the orchestrator reuses it instead of re-running.
        index = await RepoIndexService.get_or_build(
            token=request.access_token, owner=request.owner, repo=request.repo,
            branch=request.branch, include_source_corpus=True, run_repo_lens=True,
            pr_number=request.pr_number, feature_context=request.feature_context or "",
        )
        repo_context = index.repo_context
        if index.repo_lens is not None:
            repo_context["repo_lens"] = index.repo_lens
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch repository data from GitHub: {str(e)}"
        )

    try:
        # run() is synchronous and makes ~4 blocking LLM calls (~2-3 min total).
        # Calling it directly in this async route would block the event loop and
        # freeze EVERY other request for the whole analysis. Offload to a worker
        # thread — the same treatment run_stream() already gives each agent.
        # Per-request orchestrator: concurrent analyses get isolated agents.
        orchestrator = ShipMateOrchestrator.new_per_request()
        report = await asyncio.to_thread(orchestrator.run, repo_context)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Analysis pipeline failed: {str(e)}")

    # Persist the run for the history/trend dashboard. Best-effort: a storage
    # failure must not fail the analysis the caller already has in hand.
    report_store.save_report(report)

    return AnalyzeResponse(status="complete", report=report)


@router.post("/analyze/stream")
async def analyze_stream(request: AnalyzeRequest):
    """Server-Sent Events variant of /analyze: emits one event per agent as it
    completes (repo_lens → plan_forge → guardrail → testpilot), then a final
    report event. Lets the frontend render incremental progress instead of a
    blank wait screen on large repos.

    Same auth + context-build prelude as /analyze; only the orchestration is
    streamed. The final report is persisted exactly like the batch route."""
    if not request.owner or not request.repo:
        raise HTTPException(status_code=400, detail="owner and repo are required.")

    request.access_token = require_body_credential(request.access_token)

    await verify_repo_write_access(request.owner, request.repo, request.access_token)

    try:
        index = await RepoIndexService.get_or_build(
            token=request.access_token, owner=request.owner, repo=request.repo,
            branch=request.branch, include_source_corpus=True, run_repo_lens=True,
            pr_number=request.pr_number, feature_context=request.feature_context or "",
        )
        repo_context = index.repo_context
        if index.repo_lens is not None:
            repo_context["repo_lens"] = index.repo_lens
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch repository data from GitHub: {str(e)}",
        )

    # Per-request orchestrator (isolated agents) for this stream too.
    orchestrator = ShipMateOrchestrator.new_per_request()

    async def _event_source():
        try:
            async for event in orchestrator.run_stream(repo_context):
                # Persist the streamed run too, so the history dashboard sees it.
                # The report.done frame carries the assembled report as a dict;
                # rebuild a ShipMateReport from it for the store (best-effort).
                if event.get("event") == "report.done":
                    try:
                        from app.schemas.agent_schemas import ShipMateReport
                        report_store.save_report(
                            ShipMateReport.model_validate(event["report"])
                        )
                    except Exception:
                        pass
                yield f"event: {event.get('event', 'message')}\ndata: {json.dumps(event)}\n\n"
        except Exception as e:  # pragma: no cover - stream guard
            logger.exception("analyze_stream failed")
            yield f"event: error\ndata: {json.dumps({'detail': f'stream failed: {e}'})}\n\n"

    return StreamingResponse(
        _event_source(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/repos/{owner}/{repo}/history")
async def repo_history(owner: str, repo: str, limit: int = 20):
    """Return the persisted analysis-run history for owner/repo (newest first),
    so the dashboard can chart readiness-score trends over time."""
    runs = report_store.list_reports(owner, repo, limit=limit)
    return {"owner": owner, "repo": repo, "count": len(runs), "runs": runs}


@router.get("/analysis/{report_id}")
async def get_analysis(report_id: int):
    """Return a single persisted ShipMateReport by id (full detail view)."""
    rep = report_store.get_report(report_id)
    if rep is None:
        raise HTTPException(status_code=404, detail=f"No analysis run with id {report_id}.")
    return rep
