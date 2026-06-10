"""Tests for the target-repo gate that runs the patched repo's own test suite."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# Minimal stubs so the module can be imported without the full app stack
# ---------------------------------------------------------------------------

# We import the gate module under test directly; keep the import path consistent
# with however the project exposes it.
try:
    from app.services.validation_gate import (
        GateResult,
        run_target_repo_gate,
    )
except Exception:  # pragma: no cover – import guard only
    pytest.skip("validation_gate not importable", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _write_passing_suite(repo: Path) -> None:
    """Plant a minimal passing pytest suite inside *repo*."""
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_trivial.py").write_text(
        textwrap.dedent("""\
            def test_always_passes():
                assert 1 + 1 == 2
        """)
    )


def _write_failing_suite(repo: Path) -> None:
    """Plant a minimal failing pytest suite inside *repo*."""
    (repo / "tests").mkdir(parents=True, exist_ok=True)
    (repo / "tests" / "test_broken.py").write_text(
        textwrap.dedent("""\
            def test_always_fails():
                assert False, "intentional failure"
        """)
    )


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_runs_detected_pytest_in_clone(tmp_path: Path) -> None:
    """Gate passes when the cloned repo's own tests are green."""
    _write_passing_suite(tmp_path)

    res = run_target_repo_gate(repo_path=tmp_path, pytest_cmd=[sys.executable, "-m", "pytest"])
    assert res.passed is True, res.reason


def test_failing_target_tests_reject_the_patch(tmp_path: Path) -> None:
    """Gate fails (and reports >=1 failure) when the repo's tests are red."""
    _write_failing_suite(tmp_path)

    res = run_target_repo_gate(repo_path=tmp_path, pytest_cmd=[sys.executable, "-m", "pytest"])
    assert res.passed is False
    assert res.failed >= 1
