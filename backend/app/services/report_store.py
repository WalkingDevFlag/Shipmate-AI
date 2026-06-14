"""
ReportStore — durable history of analysis runs.

Each completed `/api/analyze` run is persisted here so the dashboard can show a
repo's readiness-score trend over time (turning one-shot analysis into a
longitudinal view). Mirrors the stdlib-sqlite3 convention already used by
`github_auth_service.py` (oauth_state.db) and `inflight_registry.py`
(/tmp/shipmate_inflight.db) — no new dependency, WAL mode, file-backed so it
survives restarts and is shared across workers.

Public API:
    save_report(report) -> int          # persist one run, returns row id
    list_reports(owner, repo, limit=20) # summary rows for the history dashboard
    get_report(report_id) -> dict|None   # full stored ShipMateReport JSON

The DB path defaults to /tmp/shipmate_reports.db (same wiped-on-reboot tradeoff
as the inflight registry) and is overridable via SHIPMATE_REPORTS_DB.
"""
from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

from app.services import sqlite_store

logger = logging.getLogger("shipmate.report_store")

_STORE = "reports"
_DEFAULT_DB_PATH = "/tmp/shipmate_reports.db"  # kept for docstring/back-ref only

_SCHEMA = """
CREATE TABLE IF NOT EXISTS analysis_runs (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    owner               TEXT NOT NULL,
    repo                TEXT NOT NULL,
    branch              TEXT NOT NULL,
    readiness_score     INTEGER NOT NULL,
    ship_recommendation TEXT NOT NULL,
    report_json         TEXT NOT NULL,
    created_at          TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_repo ON analysis_runs(owner, repo, id);
"""

# Register with the shared connection manager. SHIPMATE_REPORTS_DB still wins as
# a legacy override (test fixtures use it); otherwise the file lives under
# SHIPMATE_STORE_DIR (default /tmp).
sqlite_store.register(
    _STORE,
    filename="shipmate_reports.db",
    legacy_env="SHIPMATE_REPORTS_DB",
    schema=_SCHEMA,
)


def _db_path() -> str:
    """Resolved path for this store (honours SHIPMATE_REPORTS_DB override)."""
    return sqlite_store.db_path(_STORE)


def _conn() -> sqlite3.Connection:
    """Per-(thread, path) cached connection from the shared store manager."""
    return sqlite_store.connect(_STORE)


def save_report(report: Any) -> int:
    """Persist a completed ShipMateReport. Returns the new row id.

    `report` is a ShipMateReport pydantic model. We pull the dashboard-summary
    columns out for cheap querying and stash the full model JSON so a detail
    view can reconstruct everything. Never raises into the request path — a
    persistence failure must not fail the analysis the user already got."""
    try:
        repo_info = report.repo
        owner = getattr(repo_info, "owner", "") or ""
        name = getattr(repo_info, "name", "") or ""
        branch = getattr(repo_info, "branch", "") or ""
        rec = getattr(report, "ship_recommendation", "")
        rec_str = rec.value if hasattr(rec, "value") else str(rec)
        created_at = datetime.now(timezone.utc).isoformat()

        conn = _conn()
        cur = conn.execute(
            "INSERT INTO analysis_runs "
            "(owner, repo, branch, readiness_score, ship_recommendation, report_json, created_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                owner, name, branch,
                int(getattr(report, "readiness_score", 0) or 0),
                rec_str,
                report.model_dump_json(),
                created_at,
            ),
        )
        conn.commit()
        row_id = int(cur.lastrowid)
        # EvalOps metric (Phase 5): emit one event per persisted analysis so the
        # metric sink has a real producer — a ValidationSpec can assert
        # MetricExpectation("report_persisted"). Fail-open: never let a metrics
        # error break persistence.
        try:
            from app.services import metrics
            metrics.emit("report_persisted", repo=f"{owner}/{name}", score=int(getattr(report, "readiness_score", 0) or 0))
        except Exception:
            pass
        return row_id
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("report_store.save_report failed: %s", e)
        return -1


def list_reports(owner: str, repo: str, limit: int = 20) -> List[Dict[str, Any]]:
    """Summary rows for owner/repo, newest first — feeds the trend dashboard.
    Returns lightweight dicts (no full report_json) for a compact list payload."""
    try:
        limit = max(1, min(int(limit), 200))
        conn = _conn()
        rows = conn.execute(
            "SELECT id, owner, repo, branch, readiness_score, ship_recommendation, created_at "
            "FROM analysis_runs WHERE owner = ? AND repo = ? "
            "ORDER BY id DESC LIMIT ?",
            (owner, repo, limit),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("report_store.list_reports failed: %s", e)
        return []


def get_report(report_id: int) -> Optional[Dict[str, Any]]:
    """Return the full stored report (parsed JSON) for a single run, or None."""
    try:
        import json
        conn = _conn()
        row = conn.execute(
            "SELECT report_json FROM analysis_runs WHERE id = ?",
            (int(report_id),),
        ).fetchone()
        if row is None:
            return None
        return json.loads(row["report_json"])
    except Exception as e:  # pragma: no cover - defensive
        logger.warning("report_store.get_report failed: %s", e)
        return None
