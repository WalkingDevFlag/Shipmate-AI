"""
POST /api/build/plan — Phase 1A Opportunity Planner.

Returns a ranked, grounded list of self-improvement opportunities for a repo:
new features, improvements to existing ones, code-quality tweaks, and bugs —
each cited against real files and scored by value. Plan-only: NO PRs, NO Coder,
NO journal writes. This is the surface that proves opportunity quality before
Phase 1B (planning) and Phase 2 (execution) are built on top.

Same auth + context-build prelude as /analyze (you can only plan work for repos
you have write access to). Fail-open: an LLM outage returns an empty plan with
ai_enhanced=False rather than an error.
"""
import logging

import httpx
from fastapi import APIRouter, HTTPException

from app.schemas.api_schemas import (
    BuildPlanRequest, BuildExecuteRequest, BuildDismissRequest,
)
from app.schemas.agent_schemas import BuildPlanResponse, BuildExecuteResponse
from app.services.repo_analysis_service import RepoAnalysisService
from app.services.repo_index_service import RepoIndexService
from app.services.opportunity_service import OpportunityService
from app.api.deps import require_body_credential, verify_repo_write_access

router = APIRouter(tags=["build"])
logger = logging.getLogger("shipmate.build_route")


@router.post("/build/plan", response_model=BuildPlanResponse)
async def build_plan(request: BuildPlanRequest) -> BuildPlanResponse:
    if not request.owner or not request.repo:
        raise HTTPException(status_code=400, detail="owner and repo are required.")
    request.access_token = require_body_credential(request.access_token)

    # Authorization: write access required, mirroring /analyze.
    await verify_repo_write_access(request.owner, request.repo, request.access_token)

    try:
        # One front door: build_context + enrich + RepoLens, cached per
        # (owner, repo, branch, source-enriched). The planner needs the fat
        # corpus so it SEES what already exists (else it re-proposes built
        # features every run).
        index = await RepoIndexService.get_or_build(
            token=request.access_token, owner=request.owner, repo=request.repo,
            branch=request.branch, include_source_corpus=True, run_repo_lens=True,
        )
        repo_context = index.repo_context
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch repository data from GitHub: {e}",
        )

    try:
        # mode="innovation" routes to the blue-sky pipeline (discover_innovations
        # + innovation_critic); default mode="opportunity" is the conservative
        # product-review pipeline. Same context, same ranker/journal.
        if (request.mode or "").strip().lower() == "innovation":
            return OpportunityService.build_innovation_plan(
                repo_context,
                max_opportunities=request.max_opportunities,
                include_ungrounded=request.include_ungrounded,
                repo_lens=index.repo_lens,
            )
        return OpportunityService.build_plan(
            repo_context,
            max_opportunities=request.max_opportunities,
            include_ungrounded=request.include_ungrounded,
            repo_lens=index.repo_lens,
        )
    except Exception as e:
        logger.exception("build_plan failed for %s/%s", request.owner, request.repo)
        raise HTTPException(status_code=500, detail=f"Opportunity planning failed: {e}")


@router.post("/build/execute", response_model=BuildExecuteResponse)
async def build_execute(request: BuildExecuteRequest) -> BuildExecuteResponse:
    """Phase 1B: plan a chosen opportunity, critique the plan, and — when
    execute=true AND the critic approves — actuate each step through the Coder.

    execute=false (default) is a safe, free plan+critique preview. execute=true
    opens real PRs, so it requires write access (verified) and each step still
    passes CoderOrchestrator's lint/scope/pytest/resolution gates."""
    if not request.owner or not request.repo:
        raise HTTPException(status_code=400, detail="owner and repo are required.")
    request.access_token = require_body_credential(request.access_token)

    await verify_repo_write_access(request.owner, request.repo, request.access_token)

    try:
        # Single front door — build_context + enrich + RepoLens, cached. This
        # collapses the previous THREE RepoLens runs per execute (here +
        # OpportunityService.build_plan) into one. RepoLens enriches the
        # execute context (entry points for the Coder).
        index = await RepoIndexService.get_or_build(
            token=request.access_token, owner=request.owner, repo=request.repo,
            branch=request.branch, include_source_corpus=True, run_repo_lens=True,
        )
        repo_context = index.repo_context
        if index.repo_lens is not None:
            repo_context["repo_lens"] = index.repo_lens
    except Exception as e:
        raise HTTPException(
            status_code=502,
            detail=f"Failed to fetch repository data from GitHub: {e}",
        )

    try:
        return await OpportunityService.execute_opportunity(
            repo_context, request.opportunity, request.access_token,
            execute=request.execute,
        )
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=f"GitHub error: {e}")
    except Exception as e:
        logger.exception("build_execute failed for %s/%s", request.owner, request.repo)
        raise HTTPException(status_code=500, detail=f"Opportunity execution failed: {e}")


@router.get("/build/runs")
async def list_build_runs(
    owner: str | None = None, repo: str | None = None,
    status: str | None = None, limit: int = 50,
):
    """List BuildRun records (newest first), filterable by owner/repo/status.
    Read-only observability over the durable build state machine — a run is
    visible here whether it's still actuating, finished, or crashed mid-build."""
    from app.services import inflight_registry as ir
    status_in = [s.strip() for s in status.split(",")] if status else None
    runs = ir.list_build_runs(owner=owner, repo=repo, status_in=status_in, limit=limit)
    return {"count": len(runs), "runs": runs}


@router.get("/build/runs/{run_id}")
async def get_build_run(run_id: str):
    """Fetch one BuildRun by id — its status, plan, critique, accumulated PRs,
    and (when terminal) the final result. This is what survives a backend
    restart that would otherwise lose an in-flight /build/execute."""
    from app.services import inflight_registry as ir
    run = ir.get_build_run(run_id)
    if run is None:
        raise HTTPException(status_code=404, detail=f"No build run with id {run_id}.")
    return run


@router.post("/build/dismiss")
async def build_dismiss(request: BuildDismissRequest):
    """Hide an opportunity from future plans and exclude it from discovery.
    Journal-only (no GitHub mutation), so no write-access check needed — but the
    repo must be identifiable."""
    if not request.owner or not request.repo:
        raise HTTPException(status_code=400, detail="owner and repo are required.")
    full_name = f"{request.owner}/{request.repo}"
    return OpportunityService.dismiss_opportunity(
        full_name, request.title, request.file or "",
    )
