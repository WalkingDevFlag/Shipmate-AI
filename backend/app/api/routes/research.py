"""POST /api/research — the codebase research/improvement harness, streamed.

One harness, three modes, one SSE stream. It composes the engines built in
B1/B2 as a deterministic pipeline — NOT a free-form LLM tool-loop — reusing the
shared chokepoints exactly like auto_fix.py does:

  - RepoIndexService.get_or_build   (same cached context the build/analyze use)
  - ResearchService.research        (reference graph + grounded research pass)
  - OpportunityService.build_plan / build_innovation_plan  (proposal engines)

Flow (each stage emits SSE frames):
  resolve credential → build index → research (graph + findings) →
  [optionally] propose improvements (opportunity OR innovation) → done.

`mode` selects what runs:
  - "research"    : reference-graph + deep-research findings only.
  - "improve"     : research + conservative opportunity proposals.
  - "innovate"    : research + blue-sky innovation proposals.

This is PLAN/READ-ONLY: it never opens a PR or runs the Coder. Acting on a
proposal stays the explicit /api/build/execute path. Fail-open throughout — any
stage error becomes an `error` frame, never a 500 mid-stream.

SSE event shapes (JSON on a `data:` line):
  {"event": "research.start",  "mode": ...}
  {"event": "index.done",      "files": N}
  {"event": "graph.done",      "modules": N, "cycles": C, "god_modules": G}
  {"event": "finding",         "title": ..., "kind": ..., "severity": ..., "graph_signal": ...}
  {"event": "research.done",   "answer": ..., "finding_count": N}
  {"event": "propose.start",   "mode": ...}
  {"event": "opportunity",     "title": ..., "category": ..., "value_score": ..., "priority": ...}
  {"event": "propose.done",    "count": N}
  {"event": "done",            "findings": N, "opportunities": M}
  {"event": "error",           "message": ...}
"""
import json
import logging
from typing import Any, AsyncIterator, Dict

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

router = APIRouter(tags=["research"])
logger = logging.getLogger("shipmate.research_route")

_VALID_MODES = {"research", "improve", "innovate"}


class ResearchRequest(BaseModel):
    owner: str
    repo: str
    branch: str = "main"
    access_token: str
    # "research" (graph + findings only) | "improve" (+ opportunities) |
    # "innovate" (+ blue-sky innovations).
    mode: str = "research"
    question: str = ""          # optional research question ("" = open audit)
    max_findings: int = 8
    max_opportunities: int = 6


def _sse(obj: Dict[str, Any]) -> str:
    return f"data: {json.dumps(obj)}\n\n"


async def _run(req: ResearchRequest) -> AsyncIterator[str]:
    # Lazy imports: keep route registration light and load the heavy analysis
    # deps only when research is actually invoked.
    from app.api.deps import resolve_credential, verify_repo_write_access
    from app.services.repo_index_service import RepoIndexService
    from app.services.research_service import ResearchService
    from app.services.opportunity_service import OpportunityService

    mode = (req.mode or "research").strip().lower()
    if mode not in _VALID_MODES:
        mode = "research"

    resolved = resolve_credential(req.access_token)
    if not resolved:
        yield _sse({"event": "error", "message": "session expired or invalid — sign in again"})
        return
    req.access_token = resolved

    # Authorization mirrors /analyze + /build: research reads private code.
    try:
        await verify_repo_write_access(req.owner, req.repo, req.access_token)
    except Exception as e:
        yield _sse({"event": "error", "message": f"access denied: {e}"})
        return

    yield _sse({"event": "research.start", "mode": mode})

    # 1. Build (cached) context — same front door as build/analyze. Source
    # corpus is required: the reference graph + research pass read file bodies.
    try:
        index = await RepoIndexService.get_or_build(
            token=req.access_token, owner=req.owner, repo=req.repo,
            branch=req.branch, include_source_corpus=True, run_repo_lens=True,
        )
        repo_context = index.repo_context
        if index.repo_lens is not None:
            repo_context["repo_lens"] = index.repo_lens
    except Exception as e:
        logger.warning("research index build failed: %s", e)
        yield _sse({"event": "error", "message": f"failed to fetch repo: {e}"})
        return
    yield _sse({"event": "index.done", "files": len(repo_context.get("key_files") or {})})

    # 2. Research: reference graph + grounded findings (deterministic graph runs
    # even if the LLM is down — fail-open inside ResearchService).
    import asyncio
    try:
        report = await asyncio.to_thread(
            ResearchService.research, repo_context,
            question=req.question, max_findings=req.max_findings,
        )
    except Exception as e:
        logger.warning("research pass failed: %s", e)
        yield _sse({"event": "error", "message": f"research failed: {e}"})
        return

    gs = report.graph_summary or {}
    yield _sse({
        "event": "graph.done",
        "modules": gs.get("module_count", 0),
        "cycles": len(gs.get("cycles", []) or []),
        "god_modules": len(gs.get("god_modules", []) or []),
        "orphans": len(gs.get("orphans", []) or []),
    })
    for f in report.findings:
        yield _sse({
            "event": "finding", "title": f.title, "kind": f.kind,
            "severity": f.severity, "detail": f.detail,
            "evidence": f.evidence, "suggested_action": f.suggested_action,
            "graph_signal": f.graph_signal,
        })
    yield _sse({
        "event": "research.done",
        "answer": report.answer, "finding_count": len(report.findings),
        "ai_enhanced": report.ai_enhanced,
    })

    # 3. Optionally propose improvements (reusing the B1/opportunity engines).
    opp_count = 0
    if mode in ("improve", "innovate"):
        yield _sse({"event": "propose.start", "mode": mode})
        try:
            if mode == "innovate":
                plan = await asyncio.to_thread(
                    OpportunityService.build_innovation_plan, repo_context,
                    max_opportunities=req.max_opportunities, repo_lens=index.repo_lens,
                )
            else:
                plan = await asyncio.to_thread(
                    OpportunityService.build_plan, repo_context,
                    max_opportunities=req.max_opportunities, repo_lens=index.repo_lens,
                )
            for o in plan.opportunities:
                opp_count += 1
                yield _sse({
                    "event": "opportunity", "id": o.id, "title": o.title,
                    "category": o.category, "impact": o.impact, "effort": o.effort,
                    "value_score": o.value_score, "priority": o.priority,
                    "target_files": o.target_files, "rationale": o.rationale,
                })
        except Exception as e:
            logger.warning("research propose stage failed: %s", e)
            yield _sse({"event": "error", "message": f"propose failed: {e}"})
            # fall through to done — research findings still delivered.
        yield _sse({"event": "propose.done", "count": opp_count})

    yield _sse({"event": "done", "findings": len(report.findings), "opportunities": opp_count})


@router.post("/research")
async def start_research(req: ResearchRequest) -> StreamingResponse:
    """Run the research/improvement harness and stream progress as SSE. The UI
    opens this with fetch + a streaming reader (same pattern as /auto-fix/start
    and /analyze/stream)."""
    if not req.owner or not req.repo or not req.access_token:
        async def _bad() -> AsyncIterator[str]:
            yield _sse({"event": "error", "message": "owner, repo, access_token required"})
        return StreamingResponse(_bad(), media_type="text/event-stream")

    req.max_findings = max(1, min(req.max_findings, 20))
    req.max_opportunities = max(1, min(req.max_opportunities, 12))

    return StreamingResponse(
        _run(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
