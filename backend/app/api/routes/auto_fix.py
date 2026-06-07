"""
POST /api/auto-fix/start — server-side autonomous loop, streamed over SSE.

This is the UI parity for the CLI `coder_loop`: one click runs the same closed
loop the command line does — analyze → pick top findings → run_actuation
(which does lint → path-claim → pytest gate → branch/commit/PR → register the
CIWatcher) — and streams progress events to an EventSource in the browser.

It reuses the EXACT shared chokepoints, no forked logic:
  - ShipMateOrchestrator.run         (same analyze the /api/analyze route uses)
  - coder_loop._pick_top_per_kind    (same finding selection the CLI loop uses)
  - CoderOrchestrator.run_actuation  (same gates the Apply-Fix button uses)
  - finding_journal                  (same shared state)

CI-watching after a PR opens is handled inside run_actuation (it registers a
CIWatcher), so this endpoint streams up to the PR-open boundary and reports the
CIWatcher handle; the AutoFixDrawer then polls /api/watcher/... for CI status.

SSE event shapes (each is a JSON object on a `data:` line):
  {"event": "loop.start",     "rounds": N}
  {"event": "analyze.start",  "round": r}
  {"event": "analyze.done",   "round": r, "score": S, "picked": K}
  {"event": "finding.picked", "round": r, "kind": ..., "title": ...}
  {"event": "actuate.start",  "kind": ..., "title": ...}
  {"event": "actuate.done",   "kind": ..., "status": ..., "pr_url": ...}
  {"event": "round.done",     "round": r, "good": g, "skipped": s}
  {"event": "loop.done",      "good": G, "actuated": A}
  {"event": "error",          "message": ...}
  {"event": "heartbeat"}                         (keep-alive every event gap)
"""

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Dict, List, Optional

from fastapi import APIRouter
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from app.schemas.api_schemas import ActuateRequest, FindingPayload
from app.services.coder_orchestrator import CoderOrchestrator, _finding_signature
from app.services import inflight_registry as ir

router = APIRouter(tags=["auto-fix"])
logger = logging.getLogger("shipmate.auto_fix")

# Defaults mirror the CLI loop + the plan's UI parity caps.
_DEFAULT_ROUNDS = 2
_DEFAULT_MAX_FINDINGS = 4          # per round (≤1 of each kind)


class AutoFixRequest(BaseModel):
    owner: str
    repo: str
    branch: str = "main"
    access_token: str
    rounds: int = _DEFAULT_ROUNDS
    open_pr: bool = True
    # The bulk "Auto-fix" button is the do-everything path, so multi-file
    # features (milestones/blockers) should be DECOMPOSED into ordered steps
    # rather than silently skipped when one Coder call returns 0 files. The
    # decomposer fetches step-path originals so steps edit (not blind-rewrite)
    # and the scope guard still sees the originals. Off → identical to the
    # per-finding Apply-Fix button. diff_mode stays off (full-file is the
    # safer default; diff mode auto-falls back anyway).
    decompose: bool = True
    diff_mode: bool = False


def _sse(obj: Dict[str, Any]) -> str:
    """Encode one object as an SSE `data:` frame."""
    return f"data: {json.dumps(obj)}\n\n"


async def _run_loop(req: AutoFixRequest) -> AsyncIterator[str]:
    """Drive the autonomous loop, yielding SSE frames. Every exception is
    surfaced as an `error` event rather than killing the stream silently."""
    # Imported lazily so a heavy import never delays route registration / the
    # rest of the app, and so the orchestrator's analyze deps load only when
    # auto-fix is actually used.
    from app.services.repo_analysis_service import RepoAnalysisService
    from app.orchestrator.shipmate_orchestrator import ShipMateOrchestrator
    from scripts.coder_loop import _pick_top_per_kind

    repo_full = f"{req.owner}/{req.repo}"
    orchestrator = ShipMateOrchestrator()
    seen_signatures: set = set()
    cooled_paths: set = set()

    # Seed dedup from the journal so we don't re-attempt shipped/parked/
    # dismissed findings (same contract as the CLI loop's _load_state).
    try:
        for row in ir.journal_list(repo_full_name=repo_full):
            if row.get("state") in ("shipped", "dismissed", "parked"):
                seen_signatures.add(row["finding_sig"])
    except Exception as e:
        logger.info("auto-fix: journal seed skipped: %s", e)

    parked = {
        row["finding_sig"]
        for row in ir.journal_list(repo_full_name=repo_full, state_in=["parked"])
    }

    totals = {"good": 0, "actuated": 0}
    yield _sse({"event": "loop.start", "rounds": req.rounds})

    for r in range(1, req.rounds + 1):
        yield _sse({"event": "analyze.start", "round": r})
        try:
            repo_context = await RepoAnalysisService.build_context(
                token=req.access_token, owner=req.owner, repo=req.repo,
                branch=req.branch, pr_number=None, feature_context="",
            )
            report = await asyncio.to_thread(orchestrator.run, repo_context)
            report_dict = report.model_dump()
        except Exception as e:
            logger.warning("auto-fix analyze failed: %s", e)
            yield _sse({"event": "error", "message": f"analyze failed: {e}"})
            return

        score = report_dict.get("readiness_score")
        findings: List[FindingPayload] = _pick_top_per_kind(
            report_dict, seen_signatures, cooled_paths, parked,
        )
        yield _sse({
            "event": "analyze.done", "round": r,
            "score": score, "picked": len(findings),
        })

        if not findings:
            yield _sse({"event": "round.done", "round": r, "good": 0, "skipped": 0})
            yield _sse({"event": "loop.done", **totals, "note": "dry round — stopping"})
            return

        round_good = 0
        round_skipped = 0
        for f in findings:
            sig = _finding_signature(f)
            seen_signatures.add(sig)
            yield _sse({
                "event": "finding.picked", "round": r,
                "kind": f.kind, "title": f.title, "signature": sig,
            })
            yield _sse({"event": "actuate.start", "kind": f.kind, "title": f.title})

            # Decompose only multi-file kinds; a guardrail/test tweak is
            # single-file by nature and planning would just add latency.
            _decompose = req.decompose and f.kind in ("milestone", "blocker")
            actuate_req = ActuateRequest(
                owner=req.owner, repo=req.repo, branch=req.branch,
                access_token=req.access_token, finding=f, open_pr=req.open_pr,
                decompose=_decompose, diff_mode=req.diff_mode,
            )
            try:
                resp = await CoderOrchestrator.run_actuation(actuate_req)
            except Exception as e:
                logger.warning("auto-fix actuate raised for %s: %s", f.title, e)
                round_skipped += 1
                yield _sse({
                    "event": "actuate.done", "kind": f.kind, "title": f.title,
                    "status": "error", "message": str(e)[:300],
                })
                continue

            # run_actuation returns status="complete" with a pr_url on success;
            # everything else (no_change, lint_rejected, path_busy,
            # pytest_rejected) is a non-shipping outcome.
            status = getattr(resp, "status", "unknown")
            pr_url = getattr(resp, "pr_url", None)
            shipped = status == "complete" and bool(pr_url)
            if shipped:
                round_good += 1
                totals["good"] += 1
            else:
                round_skipped += 1
            totals["actuated"] += 1
            # Mark shipped findings in-flight so a follow-up round / the UI sees
            # them; CIWatcher (registered inside run_actuation) drives them to
            # shipped/gave_up from there.
            if shipped:
                try:
                    ir.journal_set_state(sig, repo_full, "in_progress", pr_url=pr_url)
                except Exception:
                    pass
            yield _sse({
                "event": "actuate.done", "kind": f.kind, "title": f.title,
                "status": status, "pr_url": pr_url,
                "files_changed": [getattr(x, "path", None) for x in getattr(resp, "files_changed", []) or []],
            })

        yield _sse({
            "event": "round.done", "round": r,
            "good": round_good, "skipped": round_skipped,
        })
        if round_good == 0 and round_skipped == len(findings):
            # Whole round produced nothing usable — stop early like the CLI.
            yield _sse({"event": "loop.done", **totals, "note": "no progress — stopping"})
            return

    yield _sse({"event": "loop.done", **totals})


@router.post("/auto-fix/start")
async def start_auto_fix(req: AutoFixRequest) -> StreamingResponse:
    """Kick off the autonomous loop and stream progress as Server-Sent Events.
    The browser opens this with fetch + a streaming reader (or EventSource via
    a GET shim); the AutoFixDrawer renders each event as it arrives."""
    if not req.owner or not req.repo or not req.access_token:
        async def _bad() -> AsyncIterator[str]:
            yield _sse({"event": "error", "message": "owner, repo, access_token required"})
        return StreamingResponse(_bad(), media_type="text/event-stream")

    # Clamp rounds to a sane ceiling so a UI bug can't launch a 100-round run.
    req.rounds = max(1, min(req.rounds, 5))

    return StreamingResponse(
        _run_loop(req),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",   # disable proxy buffering so events flush
        },
    )
