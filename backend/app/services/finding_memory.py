"""
finding_memory — SEMANTIC recurrence defense for diagnostic findings (B1).

The recurrence bug this fixes: dedup keyed on the exact `kind::title::file`
signature. GuardRail kept rephrasing the SAME finding's title every run
("Token leaked in query param" → "Access token exposed via URL parameter"),
minting a new signature each time and defeating the journal. The band-aid was
`_detect_security_controls` — a hand-maintained regex list of known controls.
That's whack-a-mole: every new recurring finding needs a new regex.

This module is the architectural fix. When a finding reaches a terminal journal
state (shipped / dismissed), we `remember()` its TEXT + an embedding VECTOR.
On the next run, `is_semantically_suppressed()` embeds the candidate and
compares it (cosine) against every remembered shipped/dismissed finding for the
repo. A rephrase of a resolved finding lands near its predecessor in vector
space and is suppressed — no matter how it's worded, and with no per-finding
regex to maintain.

Embeddings:
  • DEFAULT (always available, zero deps): a hashed token-frequency vector over
    the finding's normalized text — L2-normalized so cosine is a dot product.
    Lexical, but that's exactly what catches a rephrase: the same security
    issue described two ways shares most of its content words.
  • PLUGGABLE: set an embedder via `set_embedder(fn)` (e.g. a real embedding
    model) and remembered + query vectors both use it. The store keeps the
    embedder's `kind` tag per row so a model switch never compares vectors from
    two different spaces (mismatched-kind rows are skipped).

Persistence is the shared sqlite_store (own table, own file) so memory survives
restarts and is shared across the CLI loop + FastAPI workers. Everything is
FAIL-OPEN: any error returns "not a duplicate" — we never HIDE a finding
because the memory layer hiccuped (a false positive is annoying; a hidden real
issue is dangerous).
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from app.services import sqlite_store

logger = logging.getLogger("shipmate.finding_memory")

_STORE = "finding_memory"

# Cosine ≥ this against a remembered shipped/dismissed finding ⇒ treat the
# candidate as the same finding (a rephrase). Calibrated against measured
# separation with the lexical (word + per-word-trigram) embedder: genuine
# REPHRASES of a finding score 0.69–0.76, while DIFFERENT findings (even in the
# same security area) score 0.01–0.04 — a wide dead zone. 0.55 sits in the
# middle with large margin on both sides: no different-finding gets near it, and
# every rephrase clears it. (A real embedding model would push rephrases toward
# 0.9; the env override lets you raise the bar if you install one.) Tunable via
# SHIPMATE_FINDING_SIM_THRESHOLD.
import os as _os
_SIM_THRESHOLD = float(_os.getenv("SHIPMATE_FINDING_SIM_THRESHOLD", "0.55"))

# Journal states whose remembered findings should suppress future rephrases.
_SUPPRESSING_STATES = ("shipped", "dismissed")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS finding_memory (
    repo_full_name  TEXT NOT NULL,
    finding_sig     TEXT NOT NULL,
    kind            TEXT NOT NULL,        -- finding kind (guardrail/milestone/...)
    state           TEXT NOT NULL,        -- shipped | dismissed | ...
    title           TEXT,
    text            TEXT,                 -- normalized title+description (for debug)
    vec_kind        TEXT NOT NULL,        -- 'lexical' | embedder tag — never mix spaces
    vec_json        TEXT NOT NULL,        -- sparse {token_or_dim: weight}
    updated_at      INTEGER NOT NULL,
    PRIMARY KEY (repo_full_name, finding_sig)
);
CREATE INDEX IF NOT EXISTS idx_finding_memory_repo ON finding_memory(repo_full_name, state);
"""

sqlite_store.register(
    _STORE,
    filename="shipmate_finding_memory.db",
    legacy_env="SHIPMATE_FINDING_MEMORY_DB",
    schema=_SCHEMA,
)


def _conn() -> sqlite3.Connection:
    return sqlite_store.connect(_STORE)


def init_db() -> None:
    sqlite_store.init_schema(_STORE)


# ── Embedding (pluggable; lexical default) ───────────────────────────────────

# An embedder maps text -> (vec_kind, sparse_vector_dict). None ⇒ lexical default.
_Embedder = Callable[[str], Tuple[str, Dict[str, float]]]
_embedder: Optional[_Embedder] = None
# A custom embedder lives in a DIFFERENT cosine-score distribution than the
# lexical default (e.g. a real model pushes rephrases toward ~0.9), so reusing
# the lexical-calibrated 0.55 threshold could OVER-suppress (hide a genuinely
# different finding). set_embedder() therefore takes the threshold appropriate
# for that embedder; this holds it so the suppression path uses the matching
# bar instead of the lexical one.
_embedder_threshold: Optional[float] = None

# Stopwords that carry no discriminating signal for a finding — dropping them
# keeps the cosine focused on the substantive terms.
_STOPWORDS = frozenset("""
a an the of to in on for and or but is are be been being this that these those
it its as at by with from into via not no do does did has have had will would
should could can may might must your you our we they he she them his her their
""".split())

_TOKEN_RE = re.compile(r"[a-z0-9_]+")


def set_embedder(fn: Optional[_Embedder], *, threshold: Optional[float] = None) -> None:
    """Install a custom embedder (e.g. a real embedding model). Pass None to
    revert to the lexical default. Remembered + query vectors both use it; the
    per-row `vec_kind` tag prevents cross-space comparisons after a switch.

    `threshold`: the cosine bar to use WITH this embedder. A custom model has a
    different score distribution than the lexical default, so its bar must be set
    explicitly — otherwise we'd judge model-space similarities against the
    lexical-calibrated 0.55 and risk over-suppressing. When fn is None this
    resets to the lexical default threshold."""
    global _embedder, _embedder_threshold
    _embedder = fn
    _embedder_threshold = threshold if fn is not None else None


def _active_threshold() -> float:
    """The cosine threshold for the currently-installed embedder: the explicit
    per-embedder threshold if one was given, else the lexical-calibrated default
    (also honoring the SHIPMATE_FINDING_SIM_THRESHOLD env override)."""
    if _embedder is not None and _embedder_threshold is not None:
        return _embedder_threshold
    return _SIM_THRESHOLD


def _normalize_text(title: str, description: str = "") -> str:
    return f"{title or ''} {description or ''}".strip().lower()


# Word tokens weight more than character trigrams, but trigrams give the
# lexical vector its robustness to REPHRASING: "leaked"/"leaking" and
# "header"/"headers" share most trigrams, and "config"/"configuration" overlap —
# so a reworded finding still lands near its predecessor even when whole-word
# overlap is only partial. Pure word-frequency cosine scored real rephrases at
# ~0.73 (under threshold); blending in per-word trigrams lifts them over it
# while keeping unrelated findings low.
_WORD_WEIGHT = 1.0
_TRIGRAM_WEIGHT = 0.45


def _char_trigrams(word: str) -> List[str]:
    """3-grams of a single word, padded so short words still yield features.
    Per-WORD (not across the whole string) to avoid cross-word-boundary noise
    that would inflate similarity between unrelated findings."""
    w = f"^{word}$"
    if len(w) < 3:
        return [w]
    return [w[i:i + 3] for i in range(len(w) - 2)]


def _lexical_vector(text: str) -> Tuple[str, Dict[str, float]]:
    """L2-normalized sparse vector blending content-word frequencies with
    per-word character trigrams. Lexical, dependency-free, deterministic.
    Trigram dims are namespaced ('#tri:') so they never collide with word dims.
    Returns ('lexical', {feature: weight})."""
    counts: Dict[str, float] = {}
    for tok in _TOKEN_RE.findall(text.lower()):
        if len(tok) < 3 or tok in _STOPWORDS:
            continue
        counts[tok] = counts.get(tok, 0.0) + _WORD_WEIGHT
        for tri in _char_trigrams(tok):
            key = f"#tri:{tri}"
            counts[key] = counts.get(key, 0.0) + _TRIGRAM_WEIGHT
    norm = math.sqrt(sum(v * v for v in counts.values()))
    if norm == 0.0:
        return "lexical", {}
    return "lexical", {k: v / norm for k, v in counts.items()}


def embed(text: str) -> Tuple[str, Dict[str, float]]:
    """Embed text using the installed embedder, or the lexical default. Always
    returns (vec_kind, sparse_vector). Fail-open to lexical on embedder error."""
    if _embedder is not None:
        try:
            return _embedder(text)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("custom embedder failed (%s); using lexical", e)
    return _lexical_vector(text)


def _cosine(a: Dict[str, float], b: Dict[str, float]) -> float:
    """Cosine similarity of two sparse vectors. Both are expected L2-normalized
    (lexical default is), so this is a dot product — but we don't ASSUME it; we
    renormalize defensively so a custom embedder that returns un-normalized
    vectors still yields a correct cosine."""
    if not a or not b:
        return 0.0
    # Iterate the smaller dict for the dot product.
    small, big = (a, b) if len(a) <= len(b) else (b, a)
    dot = sum(w * big.get(k, 0.0) for k, w in small.items())
    na = math.sqrt(sum(w * w for w in a.values()))
    nb = math.sqrt(sum(w * w for w in b.values()))
    if na == 0.0 or nb == 0.0:
        return 0.0
    return dot / (na * nb)


# ── Remember ─────────────────────────────────────────────────────────────────

def remember(
    repo_full_name: str,
    finding_sig: str,
    kind: str,
    state: str,
    title: str,
    description: str = "",
) -> None:
    """Persist a finding's text + embedding under its terminal state, so future
    rephrases of it can be recognized. Idempotent upsert keyed by signature.
    No-op (fail-open) on any error or missing repo."""
    if not repo_full_name or not finding_sig:
        return
    try:
        text = _normalize_text(title, description)
        vec_kind, vec = embed(text)
        c = _conn()
        c.execute(
            "INSERT INTO finding_memory "
            "(repo_full_name, finding_sig, kind, state, title, text, "
            " vec_kind, vec_json, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo_full_name, finding_sig) DO UPDATE SET "
            "  state=excluded.state, title=excluded.title, text=excluded.text, "
            "  kind=excluded.kind, vec_kind=excluded.vec_kind, "
            "  vec_json=excluded.vec_json, updated_at=excluded.updated_at",
            (repo_full_name, finding_sig, kind, state, title[:200], text[:2000],
             vec_kind, json.dumps(vec), int(time.time())),
        )
        c.commit()
    except Exception as e:  # pragma: no cover - memory must never break callers
        logger.debug("finding_memory.remember failed (%s)", e)


def remember_detected(
    repo_full_name: str,
    finding_sig: str,
    kind: str,
    title: str,
    description: str = "",
) -> None:
    """Record a finding that was merely SURFACED (not yet shipped/dismissed) so
    yield metrics can count detections, WITHOUT making it suppress future runs.

    Two safety rules vs plain remember():
      1. State is 'detected' — NOT in _SUPPRESSING_STATES — so this never hides a
         still-real finding (an unresolved issue must keep surfacing until it is
         actually shipped or explicitly dismissed). This is the deliberate
         answer to the memory-gap design tension: observe everything, suppress
         only terminal user actions.
      2. NEVER downgrade a terminal row: if this signature is already
         shipped/dismissed, leave it (a plain remember()'s DO UPDATE would
         otherwise overwrite state='shipped' with 'detected' and silently
         re-open a suppressed finding).
    Fail-open on any error."""
    if not repo_full_name or not finding_sig:
        return
    try:
        text = _normalize_text(title, description)
        vec_kind, vec = embed(text)
        c = _conn()
        # Single atomic upsert with a guarded DO UPDATE: the WHERE clause refuses
        # to overwrite a TERMINAL (shipped/dismissed) row, so a concurrent
        # dismiss/ship that commits between a would-be read and write can't be
        # downgraded to 'detected' (the read-then-write race the review flagged).
        # New rows insert as 'detected'; existing terminal rows are left intact.
        placeholders = ",".join("?" * len(_SUPPRESSING_STATES))
        c.execute(
            "INSERT INTO finding_memory "
            "(repo_full_name, finding_sig, kind, state, title, text, "
            " vec_kind, vec_json, updated_at) "
            "VALUES (?, ?, ?, 'detected', ?, ?, ?, ?, ?) "
            "ON CONFLICT(repo_full_name, finding_sig) DO UPDATE SET "
            "  title=excluded.title, text=excluded.text, kind=excluded.kind, "
            "  vec_kind=excluded.vec_kind, vec_json=excluded.vec_json, "
            "  updated_at=excluded.updated_at "
            f"  WHERE finding_memory.state NOT IN ({placeholders})",
            (repo_full_name, finding_sig, kind, title[:200], text[:2000],
             vec_kind, json.dumps(vec), int(time.time()), *_SUPPRESSING_STATES),
        )
        c.commit()
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("finding_memory.remember_detected failed (%s)", e)


def detection_count(repo_full_name: str = "") -> int:
    """How many distinct findings have ever been detected/recorded for a repo
    (any state). Read-only; used by the yield metrics. 0 on error."""
    try:
        c = _conn()
        if repo_full_name:
            r = c.execute(
                "SELECT COUNT(*) AS n FROM finding_memory WHERE repo_full_name=?",
                (repo_full_name,),
            ).fetchone()
        else:
            r = c.execute("SELECT COUNT(*) AS n FROM finding_memory").fetchone()
        return int(r["n"]) if r else 0
    except Exception as e:  # pragma: no cover
        logger.debug("detection_count failed (%s)", e)
        return 0


# ── Query ────────────────────────────────────────────────────────────────────

def _load_suppressing(repo_full_name: str) -> List[Dict[str, Any]]:
    rows = _conn().execute(
        "SELECT * FROM finding_memory WHERE repo_full_name=? AND state IN (?, ?)",
        (repo_full_name, *_SUPPRESSING_STATES),
    ).fetchall()
    return [dict(r) for r in rows]


def nearest(
    repo_full_name: str, title: str, description: str = "",
) -> Tuple[float, Optional[Dict[str, Any]]]:
    """Return (best_similarity, best_row) of the candidate text against every
    remembered shipped/dismissed finding for the repo. (0.0, None) if memory is
    empty or anything errors (fail-open)."""
    try:
        rows = _load_suppressing(repo_full_name)
        if not rows:
            return 0.0, None
        vec_kind, qvec = embed(_normalize_text(title, description))
        best_sim, best_row = 0.0, None
        for r in rows:
            # Never compare across embedding spaces (e.g. lexical vs a model).
            if r.get("vec_kind") != vec_kind:
                continue
            try:
                rvec = json.loads(r["vec_json"])
            except Exception:
                continue
            sim = _cosine(qvec, rvec)
            if sim > best_sim:
                best_sim, best_row = sim, r
        return best_sim, best_row
    except Exception as e:  # pragma: no cover
        logger.debug("finding_memory.nearest failed (%s)", e)
        return 0.0, None


def is_semantically_suppressed(
    repo_full_name: str, title: str, description: str = "",
    *, threshold: Optional[float] = None,
) -> bool:
    """True if the candidate is a semantic rephrase of a shipped/dismissed
    finding (cosine ≥ threshold). Fail-open (False) on any error."""
    if not repo_full_name:
        return False
    thr = _active_threshold() if threshold is None else threshold
    sim, row = nearest(repo_full_name, title, description)
    if row is not None and sim >= thr:
        logger.info(
            "finding_memory: suppressing rephrase (sim=%.3f ≥ %.2f) of %r",
            sim, thr, row.get("title"),
        )
        return True
    return False


def filter_semantic_duplicates(
    findings: List[Any], repo_full_name: str,
    *, title_attr: str = "title", desc_attr: str = "description",
    threshold: Optional[float] = None,
) -> List[Any]:
    """Drop findings that are semantic rephrases of remembered shipped/dismissed
    findings. Order preserved. Fail-open: returns the input unchanged on error
    or when there's nothing remembered for the repo."""
    if not repo_full_name or not findings:
        return findings
    try:
        if not _load_suppressing(repo_full_name):
            return findings  # nothing to compare against — skip the work
    except Exception:
        return findings
    kept = []
    for f in findings:
        title = getattr(f, title_attr, "") or getattr(f, "name", "") or ""
        desc = getattr(f, desc_attr, "") or ""
        if is_semantically_suppressed(repo_full_name, title, desc, threshold=threshold):
            continue
        kept.append(f)
    return kept


# ── Test/debug helpers ───────────────────────────────────────────────────────

def reset_all() -> None:
    try:
        c = _conn()
        c.execute("DELETE FROM finding_memory")
        c.commit()
    except Exception as e:  # pragma: no cover
        logger.debug("finding_memory reset failed (%s)", e)


def count(repo_full_name: Optional[str] = None) -> int:
    try:
        if repo_full_name:
            return _conn().execute(
                "SELECT COUNT(*) FROM finding_memory WHERE repo_full_name=?",
                (repo_full_name,),
            ).fetchone()[0]
        return _conn().execute("SELECT COUNT(*) FROM finding_memory").fetchone()[0]
    except Exception:  # pragma: no cover
        return 0
