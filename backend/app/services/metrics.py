"""
metrics — a tiny pluggable metric sink (Phase 5 EvalOps).

EvalOps needs a checkable answer to "did metric X fire during this scenario?".
Rather than wire a full Prometheus/App-Insights client now, this is a local
JSONL sink: `emit(name, value, **tags)` appends one line to a file, and
`fired_names(path)` reads back which metrics were emitted. The sink path is an
env var so the eval harness can point a child process at an ISOLATED file and
read exactly the metrics that run produced — no cross-talk with other runs.

Why JSONL not sqlite (unlike the other stores): the eval harness boots the app
in a SEPARATE process/worktree and the parent reads the file afterward; a flat
append-only file is the simplest cross-process contract and is trivially
portable to a real backend later (swap `_write` for a client call).

Pluggable later: set a real sink by calling `set_sink(fn)` — `emit` then calls
it instead of (or in addition to) the file. Fail-open everywhere: a metrics
error must never break a request path.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, Dict, List, Optional

logger = logging.getLogger("shipmate.metrics")

# Default sink file. SHIPMATE_METRICS_FILE wins (the eval harness sets it to an
# isolated path); else under SHIPMATE_STORE_DIR / a repo-local reports dir.
_DEFAULT_FILENAME = "shipmate_metrics.jsonl"
_lock = threading.Lock()

# Optional pluggable sink: (name, value, tags) -> None. When set, emit() also
# calls it (the file write still happens unless disabled).
_extra_sink: Optional[Callable[[str, float, Dict[str, Any]], None]] = None
_file_enabled = True


def sink_path() -> str:
    """Resolve the metrics file path at call time (so the eval harness's env
    override is honoured by a child process)."""
    explicit = os.getenv("SHIPMATE_METRICS_FILE")
    if explicit:
        return explicit
    base = os.getenv("SHIPMATE_STORE_DIR", "/tmp")
    return os.path.join(base, _DEFAULT_FILENAME)


def set_sink(fn: Optional[Callable[[str, float, Dict[str, Any]], None]],
             *, file_enabled: bool = True) -> None:
    """Install a pluggable sink (e.g. a real metrics client). `file_enabled`
    keeps/disables the JSONL write alongside it. Pass fn=None to clear."""
    global _extra_sink, _file_enabled
    _extra_sink = fn
    _file_enabled = file_enabled


def emit(name: str, value: float = 1.0, **tags: Any) -> None:
    """Record one metric event. Append-only JSONL line + optional extra sink.
    Fail-open: never raises into the caller's request path."""
    rec = {"name": name, "value": value, "tags": tags}
    try:
        if _file_enabled:
            path = sink_path()
            with _lock:
                os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
                with open(path, "a", encoding="utf-8") as f:
                    f.write(json.dumps(rec) + "\n")
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("metrics.emit file write failed (%s)", e)
    if _extra_sink is not None:
        try:
            _extra_sink(name, value, dict(tags))
        except Exception as e:  # pragma: no cover - fail-open
            logger.debug("metrics extra sink failed (%s)", e)


def read_events(path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Read all metric events from the sink file (or `path`). Empty list when
    the file is absent. Fail-open."""
    p = path or sink_path()
    events: List[Dict[str, Any]] = []
    try:
        if not os.path.exists(p):
            return []
        with open(p, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("metrics.read_events failed (%s)", e)
    return events


def fired_names(path: Optional[str] = None) -> set:
    """Set of distinct metric names emitted to the sink file. The eval harness
    uses this to satisfy MetricExpectation.must_fire."""
    return {e.get("name") for e in read_events(path) if e.get("name")}


def reset(path: Optional[str] = None) -> None:
    """Truncate the sink file (test/eval isolation)."""
    p = path or sink_path()
    try:
        if os.path.exists(p):
            os.remove(p)
    except Exception as e:  # pragma: no cover
        logger.debug("metrics.reset failed (%s)", e)
