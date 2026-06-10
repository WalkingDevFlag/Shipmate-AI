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

# We import the gate module under test directly; keep the import path
# consistent with however the project exposes it.
try:
    from app.services.validation_gate import TargetRepoGate, GateResult
except Exception:  # pragma: no cover – collected only when app is present
    pytest.skip("validation_gate not importable", allow_module_level=True)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_simple_repo(tmp_path: Path) -> Path:
    """Create a minimal Python project with one passing test."""
    (tmp_path / "mymod.py").write_text("def add(a, b): return a + b\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_mymod.py").write_text(
        textwrap.dedent("""\
            from mymod import add

            def test_add():
                assert add(1, 2) == 3
        """)
    )
    return tmp_path


def _make_failing_repo(tmp_path: Path) -> Path:
    """Create a minimal Python project with one failing test."""
    (tmp_path / "mymod.py").write_text("def add(a, b): return a + b\n")
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "__init__.py").write_text("")
    (tests_dir / "test_mymod.py").write_text(
        textwrap.dedent("""\
            from mymod import add

            def test_add_wrong():
                assert add(1, 2) == 99  # intentionally wrong
        """)
    )
    return tmp_path


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

def test_runs_detected_pytest_in_clone(tmp_path):
    repo_dir = _make_simple_repo(tmp_path)
    gate = TargetRepoGate(repo_path=repo_dir)
    res: GateResult = gate.run()
    assert res.passed is True, res.reason


def test_passing_target_tests_accept_the_patch(tmp_path):
    repo_dir = _make_simple_repo(tmp_path)
    gate = TargetRepoGate(repo_path=repo_dir)
    res: GateResult = gate.run()
    assert res.passed is True
    assert res.failed == 0


def test_failing_target_tests_reject_the_patch(tmp_path):
    repo_dir = _make_failing_repo(tmp_path)
    gate = TargetRepoGate(repo_path=repo_dir)
    res: GateResult = gate.run()
    assert res.passed is False
    assert res.failed >= 1
