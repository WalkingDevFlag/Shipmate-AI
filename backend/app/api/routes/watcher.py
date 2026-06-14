"""
GET    /api/watcher                                   — list active CI watchers
GET    /api/watcher/{owner}/{repo}/{pr_number}        — single watcher state
GET    /api/watcher/{owner}/{repo}/{pr_number}/log    — tail the watcher log
DELETE /api/watcher/{owner}/{repo}/{pr_number}        — stop watching

The frontend polls /api/watcher/{...} every ~10-15s to render a live
"PRs under auto-fix" panel (ActuateButton's expanded CIWatchPanel). The
/log endpoint paginates on `since_id` so the UI appends only new lines.

No auth required to read state — responses carry finding metadata and
log lines only; the access_token lives in sqlite and is never echoed
back (get_log_tail / list_active project token-less columns).
"""

import logging
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Header, HTTPException, Query

from app.services import inflight_registry as ir
from app.services.ci_watcher import CIWatcher

router = APIRouter(tags=["watcher"])
logger = logging.getLogger("shipmate.watcher_route")


@router.get("/watcher")
async def list_watchers() -> Dict[str, List[Dict[str, Any]]]:
    return {"active": CIWatcher.list_active()}


@router.get("/watcher/{owner}/{repo}/{pr_number}")
async def get_watcher(owner: str, repo: str, pr_number: int) -> Dict[str, Any]:
    state = CIWatcher.get(owner, repo, pr_number)
    if state is None:
        raise HTTPException(status_code=404, detail="not watching this PR")
    return state


@router.get("/watcher/{owner}/{repo}/{pr_number}/log")
async def get_watcher_log(
    owner: str, repo: str, pr_number: int,
    since_id: int = Query(0, ge=0),
) -> Dict[str, Any]:
    """Return log lines with id > since_id (capped). The UI advances
    since_id to the returned next_id and re-polls — append-only tail."""
    try:
        return ir.get_log_tail(owner, repo, pr_number, since_id=since_id)
    except Exception as e:
        logger.warning("get_watcher_log failed: %s", e)
        raise HTTPException(status_code=500, detail=f"log read failed: {e}")


@router.delete("/watcher/{owner}/{repo}/{pr_number}")
async def stop_watcher(
    owner: str,
    repo: str,
    pr_number: int,
    authorization: Optional[str] = Header(None),
) -> Dict[str, bool]:
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Missing or invalid Authorization header")
    return {"stopped": CIWatcher.stop(owner, repo, pr_number)}
