"""
GET  /api/branches/{owner}/{repo}/prune?dry_run=true   — preview deletions
POST /api/branches/{owner}/{repo}/prune                — actually delete

Token is supplied via Authorization: Bearer <token> header.
"""

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, Depends, HTTPException

from app.services.branch_pruner import prune_shipmate_branches
from app.api.deps import resolve_access_token, verify_repo_write_access

router = APIRouter(tags=["branches"])
logger = logging.getLogger("shipmate.branches_route")


@router.get("/branches/{owner}/{repo}/prune")
async def preview_prune(
    owner: str, repo: str, access_token: str = Depends(resolve_access_token),
):
    """Dry-run: returns what WOULD be deleted without touching anything.

    Token resolved from the Authorization header (preferred) or legacy query
    param — the query-param form leaks the token into logs/Referer, so the
    header is preferred (OPP-001; this route previously only accepted Query)."""
    try:
        report = await prune_shipmate_branches(access_token, owner, repo, dry_run=True)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("prune dry-run failed for %s/%s", owner, repo)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return report.to_dict()


@router.post("/branches/{owner}/{repo}/prune")
async def execute_prune(
    owner: str, repo: str, access_token: str = Depends(resolve_access_token),
):
    """Actually delete the qualifying branches. Verifies write access first —
    this is a destructive operation, so a read-only token must not reach it."""
    await verify_repo_write_access(owner, repo, access_token)
    try:
        report = await prune_shipmate_branches(access_token, owner, repo, dry_run=False)
    except httpx.HTTPStatusError as e:
        raise HTTPException(status_code=502, detail=str(e)) from e
    except Exception as e:
        logger.exception("prune execute failed for %s/%s", owner, repo)
        raise HTTPException(status_code=500, detail=str(e)) from e
    return report.to_dict()
