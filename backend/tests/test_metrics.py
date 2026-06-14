"""metrics — the pluggable JSONL metric sink (Phase 5 EvalOps).

Pin: emit appends, fired_names reads back distinct names, the env override
isolates the sink file, reset truncates, a pluggable extra sink is called, and
everything is fail-open.
"""
import json
import os

import pytest

from app.services import metrics


@pytest.fixture
def isolated_sink(tmp_path, monkeypatch):
    p = str(tmp_path / "m.jsonl")
    monkeypatch.setenv("SHIPMATE_METRICS_FILE", p)
    metrics.reset(p)
    yield p
    metrics.set_sink(None)  # clear any extra sink a test installed


def test_emit_and_fired_names(isolated_sink):
    metrics.emit("report_persisted")
    metrics.emit("analyze_run", value=2.0, repo="o/r")
    metrics.emit("report_persisted")  # again — distinct names dedupe
    names = metrics.fired_names()
    assert names == {"report_persisted", "analyze_run"}


def test_read_events_has_tags(isolated_sink):
    metrics.emit("x", value=3.0, foo="bar")
    events = metrics.read_events()
    assert len(events) == 1
    assert events[0]["name"] == "x"
    assert events[0]["value"] == 3.0
    assert events[0]["tags"] == {"foo": "bar"}


def test_env_override_isolates_sink(tmp_path, monkeypatch):
    p = str(tmp_path / "iso.jsonl")
    monkeypatch.setenv("SHIPMATE_METRICS_FILE", p)
    metrics.reset(p)
    metrics.emit("only_here")
    assert metrics.sink_path() == p
    assert "only_here" in metrics.fired_names(p)


def test_reset_truncates(isolated_sink):
    metrics.emit("a")
    assert metrics.fired_names() == {"a"}
    metrics.reset()
    assert metrics.fired_names() == set()


def test_pluggable_extra_sink_called(isolated_sink):
    seen = []
    metrics.set_sink(lambda name, value, tags: seen.append((name, value, tags)))
    metrics.emit("piped", value=1.0, k="v")
    assert seen == [("piped", 1.0, {"k": "v"})]
    # file write still happened (file_enabled default True)
    assert "piped" in metrics.fired_names()


def test_read_missing_file_is_empty(tmp_path):
    assert metrics.read_events(str(tmp_path / "nope.jsonl")) == []
    assert metrics.fired_names(str(tmp_path / "nope.jsonl")) == set()


def test_emit_never_raises(monkeypatch):
    # Even if the file path is unwritable, emit must not raise.
    monkeypatch.setenv("SHIPMATE_METRICS_FILE", "/this/path/cannot/exist/x.jsonl")
    metrics.emit("safe")  # should swallow the error
