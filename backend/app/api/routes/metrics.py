"""
Metrics route — the yield dashboard (read-only).

GET /api/metrics/yield  → "is the harness net-positive?" — acceptance rate,
dismissal rate, gate-rejection breakdown, spend-per-shipped. Joins
finding_journal + coder_lessons + finding_memory + run_trace via
yield_metrics.compute_yield.

Read-only and credential-free: it exposes only AGGREGATE counts (no finding
text, no tokens, no repo content), so it needs no auth gate — same posture as a
health/metrics endpoint. Scope with ?repo=owner/name; omit for global.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

from fastapi import APIRouter, Query

from app.services import yield_metrics

logger = logging.getLogger("shipmate.metrics_route")

router = APIRouter(tags=["metrics"])


@router.get("/metrics/yield")
async def get_yield(
    repo: Optional[str] = Query(
        None, description="owner/repo to scope the metrics; omit for global."
    ),
) -> Dict[str, Any]:
    """Aggregate yield metrics. Fail-open: returns zeros (never 500s) if a store
    is unavailable, so the dashboard is always reachable."""
    try:
        return yield_metrics.compute_yield(repo)
    except Exception as e:  # pragma: no cover - the service is already fail-open
        logger.warning("yield metrics failed (%s)", e)
        return {"repo": repo or "(global)", "error": "metrics temporarily unavailable"}
