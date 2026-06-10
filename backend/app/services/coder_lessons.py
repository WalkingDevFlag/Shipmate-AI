"""
coder_lessons — per-repo memory of WHY Coder patches were rejected (B2 / C4).

The gap: the finding journal records WHAT happened to a finding (in_progress /
shipped / dismissed / parked) but never WHY a Coder patch failed. So the Coder
re-hallucinates the same import (`from app.services.auth import …` that doesn't
exist) every few runs, re-commits the same scope creep, re-breaks the same
tests — with zero memory. Each rejection is paid for again from scratch.

This module closes the loop. The CoderOrchestrator already rejects patches at
named gates (lint_rejected / scope_rejected / pytest_rejected /
resolution_failed). When it does, it calls `record_failure()` with the gate +
the concrete issue text. We DISTILL those raw issues into short, durable
"lessons" (e.g. "imports must resolve to real modules — a prior patch
hallucinated `app.services.auth`") and persist them per repo. Before the next
Coder call, `lessons_digest()` returns the top recurring lessons for that repo,
which the orchestrator injects into the brief: "Prior patches to THIS repo
failed because: …". The Coder learns the repo instead of re-failing.

Distillation is deterministic (pattern → canonical lesson) so it needs no LLM
and is cheap to call on every rejection. Lessons are ranked by recurrence
(hit_count) so the digest surfaces the mistakes the model ACTUALLY keeps
making, and capped so the brief stays small.

Persistence: shared sqlite_store (own table, own file). Survives restarts,
shared across the CLI loop + FastAPI. FAIL-OPEN everywhere — a lessons write or
read failure must never break an actuate.
"""
from __future__ import annotations

import logging
import re
import sqlite3
import time
from typing import Dict, List, Optional, Tuple

from app.services import sqlite_store

logger = logging.getLogger("shipmate.coder_lessons")

_STORE = "coder_lessons"
_MAX_DIGEST_LESSONS = 6        # keep the brief injection small
_MIN_HITS_FOR_DIGEST = 1       # surface a lesson after a single failure

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coder_lessons (
    repo_full_name  TEXT NOT NULL,
    lesson_key      TEXT NOT NULL,     -- canonical key so re-failures aggregate
    lesson          TEXT NOT NULL,     -- human-readable instruction for the model
    gate            TEXT,              -- lint | scope | pytest | resolution | other
    hit_count       INTEGER NOT NULL DEFAULT 1,
    last_detail     TEXT,              -- most recent raw issue (for debugging)
    first_seen_at   INTEGER NOT NULL,
    last_seen_at    INTEGER NOT NULL,
    PRIMARY KEY (repo_full_name, lesson_key)
);
CREATE INDEX IF NOT EXISTS idx_coder_lessons_repo ON coder_lessons(repo_full_name, hit_count);
"""

sqlite_store.register(
    _STORE,
    filename="shipmate_coder_lessons.db",
    legacy_env="SHIPMATE_CODER_LESSONS_DB",
    schema=_SCHEMA,
)


def _conn() -> sqlite3.Connection:
    return sqlite_store.connect(_STORE)


def init_db() -> None:
    sqlite_store.init_schema(_STORE)


# ── Distillation: raw gate issue → canonical lesson ──────────────────────────
# Each rule maps a detected pattern in the raw rejection text to a (key, lesson)
# pair. The key aggregates re-failures of the SAME class; the lesson is the
# instruction injected into the next brief. Ordered most-specific first.

_HALLUCINATED_MODULE_RE = re.compile(
    r"hallucinated[^:]*imports?[^:]*:\s*(.+)", re.IGNORECASE
)
_STALE_SYMBOL_RE = re.compile(
    r"imports symbols that don't exist[^:]*:\s*(.+)", re.IGNORECASE
)
_SYNTAX_RE = re.compile(r"syntax\s*error|unparseable|invalid syntax", re.IGNORECASE)


def distill(gate: str, issue: str) -> Tuple[str, str]:
    """Map one raw rejection issue to a (lesson_key, lesson_text). Deterministic,
    no LLM. The key is coarse enough that the SAME class of mistake aggregates
    across runs, but carries the offending symbol when we can extract it so the
    lesson is concrete ('don't import app.services.auth' beats 'imports must be
    real')."""
    text = issue or ""
    low = text.lower()

    m = _HALLUCINATED_MODULE_RE.search(text)
    if m or "hallucinated" in low:
        offenders = _extract_symbols(m.group(1) if m else text)
        key = "hallucinated-import" + (f":{offenders[0]}" if offenders else "")
        detail = (f"a prior patch invented the import(s) {', '.join(offenders)} "
                  if offenders else "a prior patch invented imports ")
        return key, (
            f"IMPORT FIDELITY: {detail}— they do not exist in this repo. Only "
            "import symbols present in the shown target_files, the repo's "
            "entry_points, or the Python stdlib. Never guess a module path."
        )

    m = _STALE_SYMBOL_RE.search(text)
    if m or ("imports symbols" in low and "don't exist" in low):
        offenders = _extract_symbols(m.group(1) if m else text)
        key = "stale-symbol" + (f":{offenders[0]}" if offenders else "")
        detail = (f"the symbol(s) {', '.join(offenders)} " if offenders
                  else "some imported symbols ")
        return key, (
            f"SYMBOL FIDELITY: {detail}are imported but not defined in the target "
            "module(s). Import only names that actually exist in the files shown."
        )

    if _SYNTAX_RE.search(text):
        return "python-syntax", (
            "SYNTAX: a prior patch produced unparseable Python. Return complete, "
            "valid file content — no diff markers, ellipses, or truncation."
        )

    if gate == "scope" or "scope" in low or "drop" in low or "unrelated" in low:
        return "scope-drift", (
            "SCOPE DISCIPLINE: a prior patch was rejected for scope drift — it "
            "rewrote or dropped code unrelated to the finding. Touch only what "
            "the finding requires; preserve every unrelated import/route/def "
            "from the original verbatim."
        )

    # Eval gate (P5) — check BEFORE pytest so an EvalOps message that happens to
    # contain the substring 'test' isn't miscategorized as a pytest regression.
    if gate == "eval" or "evalops" in low or "acceptance spec" in low or "scenario" in low:
        return "eval-acceptance", (
            "ACCEPTANCE: a prior patch passed lint/tests but FAILED its EvalOps "
            "acceptance spec — the change did not observably WORK against the "
            "running app (a scenario/metric/log assertion failed). Make the "
            "feature actually function end-to-end, not just compile and pass unit tests."
        )

    if gate == "pytest" or "test" in low or "regression" in low:
        return "pytest-regression", (
            "TEST SAFETY: a prior patch broke the test suite. Run the change "
            "through the existing tests in your head — do not remove, weaken, or "
            "break tests; if behaviour changes, update the affected test in the "
            "same patch."
        )

    if gate == "resolution" or "offending pattern" in low or "still present" in low:
        return "unresolved-finding", (
            "ACTUALLY FIX IT: a prior patch passed tests but left the flagged "
            "pattern in place. Make sure the change OBSERVABLY removes the issue "
            "the finding describes, not just compiles."
        )

    if gate == "phantom" or "empty patch" in low or "no files" in low:
        return "phantom-patch", (
            "NO PHANTOM PATCHES: a prior response returned ZERO file edits while "
            "the summary narrated a fix (often with a clean VERIFY line). If you "
            "make a change, emit the actual file content; if you genuinely "
            "cannot, set the summary to 'DECLINED: <reason>' — never stamp a "
            "clean VERIFY on an empty patch."
        )

    # Fallback — keep the gate as the key so repeated generic failures still
    # aggregate, but with a less specific lesson.
    return f"{gate or 'other'}-generic", (
        f"A prior patch was rejected at the {gate or 'review'} gate: "
        f"{text[:160]}. Avoid repeating this."
    )


_SYMBOL_RE = re.compile(r"[a-zA-Z_][a-zA-Z0-9_.]+")


def _extract_symbols(blob: str, limit: int = 3) -> List[str]:
    """Pull dotted module/symbol names out of a raw issue string (e.g.
    "['app.services.auth', 'app.db.session']"). Best-effort; returns the most
    plausible offenders (those containing a dot or a leading app/src package)."""
    if not blob:
        return []
    cands = []
    for tok in _SYMBOL_RE.findall(blob):
        if "." in tok or tok.startswith(("app", "src", "backend", "frontend")):
            cands.append(tok.strip("."))
    # Dedupe preserving order.
    seen, out = set(), []
    for c in cands:
        if c not in seen:
            seen.add(c)
            out.append(c)
        if len(out) >= limit:
            break
    return out


# ── Record ───────────────────────────────────────────────────────────────────

def record_failure(
    repo_full_name: str, gate: str, issues: List[str] | str,
) -> None:
    """Distill each rejection issue into a canonical lesson and upsert it for the
    repo, bumping hit_count on a repeat. `issues` may be a list (lint/scope
    return lists) or a single string (pytest reason). No-op (fail-open) on any
    error or missing repo."""
    if not repo_full_name:
        return
    issue_list = issues if isinstance(issues, list) else [issues]
    issue_list = [i for i in issue_list if i]
    if not issue_list:
        # Still record the gate so we know it failed, even with no detail.
        issue_list = [f"{gate} rejection (no detail)"]
    try:
        now = int(time.time())
        c = _conn()
        for issue in issue_list:
            key, lesson = distill(gate, str(issue))
            c.execute(
                "INSERT INTO coder_lessons "
                "(repo_full_name, lesson_key, lesson, gate, hit_count, "
                " last_detail, first_seen_at, last_seen_at) "
                "VALUES (?, ?, ?, ?, 1, ?, ?, ?) "
                "ON CONFLICT(repo_full_name, lesson_key) DO UPDATE SET "
                "  hit_count = hit_count + 1, last_detail = excluded.last_detail, "
                "  lesson = excluded.lesson, last_seen_at = excluded.last_seen_at",
                (repo_full_name, key, lesson, gate, str(issue)[:400], now, now),
            )
        c.commit()
    except Exception as e:  # pragma: no cover - lessons must never break actuate
        logger.debug("coder_lessons.record_failure failed (%s)", e)


# ── Digest (injected into the Coder brief) ───────────────────────────────────

def top_lessons(
    repo_full_name: str, limit: int = _MAX_DIGEST_LESSONS,
) -> List[Dict]:
    """The most-recurring lessons for a repo (highest hit_count first), capped.
    Empty list on any error or missing repo (fail-open)."""
    if not repo_full_name:
        return []
    try:
        rows = _conn().execute(
            "SELECT lesson, gate, hit_count, last_detail FROM coder_lessons "
            "WHERE repo_full_name=? AND hit_count >= ? "
            "ORDER BY hit_count DESC, last_seen_at DESC LIMIT ?",
            (repo_full_name, _MIN_HITS_FOR_DIGEST, max(1, limit)),
        ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:  # pragma: no cover
        logger.debug("coder_lessons.top_lessons failed (%s)", e)
        return []


def gate_breakdown(repo_full_name: Optional[str] = None) -> Dict[str, int]:
    """Total rejections grouped by gate (lint/scope/pytest/resolution/eval/other),
    summed over hit_count. This is the false-positive / wasted-actuation proxy
    for the yield dashboard: a Coder patch rejected at a gate is work that
    didn't ship. Empty dict on error (fail-open)."""
    try:
        if repo_full_name:
            rows = _conn().execute(
                "SELECT gate, SUM(hit_count) AS n FROM coder_lessons "
                "WHERE repo_full_name=? GROUP BY gate", (repo_full_name,),
            ).fetchall()
        else:
            rows = _conn().execute(
                "SELECT gate, SUM(hit_count) AS n FROM coder_lessons GROUP BY gate",
            ).fetchall()
        return {(r["gate"] or "other"): int(r["n"]) for r in rows}
    except Exception as e:  # pragma: no cover
        logger.debug("coder_lessons.gate_breakdown failed (%s)", e)
        return {}


def lessons_digest(repo_full_name: str, limit: int = _MAX_DIGEST_LESSONS) -> str:
    """A compact, brief-ready block of the repo's recurring Coder lessons, or ""
    when there are none. Injected into the Coder user prompt so the model stops
    repeating this repo's specific past mistakes."""
    lessons = top_lessons(repo_full_name, limit)
    if not lessons:
        return ""
    lines = [
        "# LESSONS FROM PRIOR PATCHES TO THIS REPO — do NOT repeat these mistakes:"
    ]
    for ln in lessons:
        hits = ln.get("hit_count", 1)
        suffix = f" (seen {hits}×)" if hits > 1 else ""
        lines.append(f"  - {ln['lesson']}{suffix}")
    return "\n".join(lines)


# ── Test/debug helpers ───────────────────────────────────────────────────────

def reset_all() -> None:
    try:
        c = _conn()
        c.execute("DELETE FROM coder_lessons")
        c.commit()
    except Exception as e:  # pragma: no cover
        logger.debug("coder_lessons reset failed (%s)", e)


def count(repo_full_name: Optional[str] = None) -> int:
    try:
        if repo_full_name:
            return _conn().execute(
                "SELECT COUNT(*) FROM coder_lessons WHERE repo_full_name=?",
                (repo_full_name,),
            ).fetchone()[0]
        return _conn().execute("SELECT COUNT(*) FROM coder_lessons").fetchone()[0]
    except Exception:  # pragma: no cover
        return 0
