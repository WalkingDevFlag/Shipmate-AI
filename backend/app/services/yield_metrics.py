"""
yield_metrics — answer "is the harness net-positive?" (the missing dashboard).

We already measure SPEND (run_trace approx_tokens) but never YIELD: how many
findings get accepted (shipped) vs dismissed vs rejected at a gate. The data
exists across three stores — finding_journal (terminal states), coder_lessons
(gate rejections), finding_memory (detections) — but was never aggregated.

This module joins them into one read-only summary the /api/metrics/yield route
serves. Pure reads, fully fail-open: a broken store contributes zeros, never an
exception, so the dashboard degrades gracefully instead of 500ing.

Definitions (documented so the numbers aren't misread):
  • detected        — distinct findings ever surfaced (finding_memory rows).
  • shipped         — finding_journal state 'shipped' (a PR opened).
  • dismissed       — finding_journal state 'dismissed' (user said "not real").
  • in_progress/parked — mid-flight / shelved journal states.
  • acceptance_rate — shipped / (shipped + dismissed + parked)  [terminal-decided].
  • dismissal_rate  — dismissed / (terminal-decided). High ⇒ noisy detection.
  • gate_rejections — Coder patches rejected per gate (lint/scope/pytest/eval).
                      A false-positive / wasted-actuation proxy.
"""
from __future__ import annotations

import logging
from typing import Any, Dict, Optional

logger = logging.getLogger("shipmate.yield_metrics")


def _journal_counts(repo_full_name: Optional[str]) -> Dict[str, int]:
    try:
        from app.services import inflight_registry as ir
        rows = ir.journal_list(repo_full_name=repo_full_name)
        counts: Dict[str, int] = {}
        for r in rows:
            st = r.get("state", "unknown")
            counts[st] = counts.get(st, 0) + 1
        return counts
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("yield: journal counts failed (%s)", e)
        return {}


def compute_yield(repo_full_name: Optional[str] = None) -> Dict[str, Any]:
    """Aggregate the yield summary. `repo_full_name` scopes it; omit for global.
    Never raises."""
    journal = _journal_counts(repo_full_name)
    shipped = journal.get("shipped", 0)
    dismissed = journal.get("dismissed", 0)
    parked = journal.get("parked", 0)
    in_progress = journal.get("in_progress", 0)
    terminal = shipped + dismissed + parked

    def _rate(num: int) -> Optional[float]:
        return round(num / terminal, 3) if terminal else None

    try:
        from app.services import finding_memory as fm
        detected = fm.detection_count(repo_full_name or "")
    except Exception:  # pragma: no cover
        detected = 0

    try:
        from app.services import coder_lessons
        gate_rejections = coder_lessons.gate_breakdown(repo_full_name)
    except Exception:  # pragma: no cover
        gate_rejections = {}

    try:
        from app.services import run_trace
        recent = run_trace.recent_runs(20)
        spend = sum(run_trace.run_summary(r).get("approx_tokens", 0) for r in recent)
    except Exception:  # pragma: no cover
        recent, spend = [], 0

    total_rejections = sum(gate_rejections.values())

    return {
        "repo": repo_full_name or "(global)",
        "detected": detected,
        "journal": {
            "shipped": shipped,
            "dismissed": dismissed,
            "parked": parked,
            "in_progress": in_progress,
        },
        "acceptance_rate": _rate(shipped),
        "dismissal_rate": _rate(dismissed),
        "gate_rejections": gate_rejections,
        "total_gate_rejections": total_rejections,
        "spend": {
            "recent_runs": len(recent),
            "approx_tokens": spend,
            "approx_tokens_per_shipped": round(spend / shipped, 1) if shipped else None,
        },
        "interpretation": _interpret(shipped, dismissed, terminal, total_rejections),
    }


def _interpret(shipped: int, dismissed: int, terminal: int, rejections: int) -> str:
    """One honest human-readable line — no spin. The whole point of the
    dashboard is to say plainly whether the harness is paying off."""
    if terminal == 0 and rejections == 0:
        return "No terminal outcomes yet — not enough data to judge net value."
    if shipped == 0 and (dismissed or rejections):
        return ("Zero shipped so far while findings were dismissed/rejected — "
                "detection or generation is not yet converting to merged work.")
    ratio = (shipped / terminal) if terminal else 0
    if ratio >= 0.5:
        return f"Net-positive signal: {shipped}/{terminal} terminal findings shipped."
    return (f"Mixed: {shipped}/{terminal} shipped, {dismissed} dismissed, "
            f"{rejections} gate-rejections — detection noise or gate friction is high.")
