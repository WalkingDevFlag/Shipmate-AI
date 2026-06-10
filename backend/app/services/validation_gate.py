"""
ValidationGate — local pytest gate shared between the CLI loop and the
UI orchestrator.

Extracted from backend/scripts/coder_loop.py so a UI 'Apply Fix' click
gets the SAME safety net as the standalone loop (snapshot → apply →
smoke import → pytest → revert on regression).

Three callers, identical semantics:
  • coder_loop.AutonomousLoop  (CLI, sequential per finding)
  • CoderOrchestrator.run_actuation  (UI / API, per request)
  • Tier-2 Decomposer step-by-step actuation (each step gates separately)

Why the local working tree (not a tempdir clone):
  This is the same trade-off the standalone loop already makes: a clone
  per call costs 5-10s and requires git CLI; the snapshot/restore pattern
  has been stable through 24 rounds of dogfooding. The user explicitly
  picked this option in the plan-mode questionnaire.

Key invariants:
  1. snapshot_files MUST be called before apply, restore_snapshot MUST
     run on every failure path. Use a try/finally at the call site.
  2. baseline_pass_count is shared via InflightRegistry.meta_kv — the
     CLI loop and the FastAPI process see the same number.
  3. Pass count is the gate signal. We only fail when passed_after <
     passed_before (regression). passed_after == passed_before is fine
     (no regression even if some tests fail) and passed_after > before
     bumps the baseline.
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from app.services import inflight_registry as ir

logger = logging.getLogger("shipmate.validation_gate")

# The interpreter running THIS process — correct in every environment:
# local venv (backend/venv/bin/python), the hosted Oryx container
# (/workspace/pythonenv3.11/bin/python), and CI. Hardcoding './venv/bin/python'
# broke actuation on Azure, where no ./venv exists relative to the CWD.
PYTHON = sys.executable

# Repo root resolved once at import: this file lives at
# backend/app/services/validation_gate.py — go up three levels to repo root.
REPO_ROOT = Path(__file__).resolve().parents[3]
BACKEND_DIR = REPO_ROOT / "backend"
PYTEST_TIMEOUT_S = 90
SMOKE_TIMEOUT_S = 10
_BASELINE_KEY = "validation_gate.baseline_passing"


# ── Snapshot / restore ──────────────────────────────────────────────────────

def snapshot_files(paths: List[str]) -> Dict[str, Optional[str]]:
    """Capture current contents of `paths` so we can revert. None means
    'didn't exist' (and revert should delete on rollback)."""
    snap: Dict[str, Optional[str]] = {}
    for p in paths:
        full = REPO_ROOT / p
        if full.exists():
            try:
                snap[p] = full.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                logger.warning("snapshot_files: could not read %s: %s", p, e)
                snap[p] = None
        else:
            snap[p] = None
    return snap


def restore_snapshot(snap: Dict[str, Optional[str]]) -> None:
    """Write the snapshot back. None entries → delete the file on disk."""
    for p, original in snap.items():
        full = REPO_ROOT / p
        try:
            if original is None:
                if full.exists():
                    full.unlink()
            else:
                full.parent.mkdir(parents=True, exist_ok=True)
                full.write_text(original)
        except Exception as e:
            logger.error("restore_snapshot: could not restore %s: %s", p, e)


def write_files_to_tree(files: List[Dict[str, str]]) -> List[str]:
    """Apply Coder output files to the local tree. Returns list of paths
    actually written (skips no-ops where the existing content already
    matches). Refuses absolute paths and traversal.

    `files` is `[{path, new_content}, ...]` — same shape both the loop's
    `_files_full` and the orchestrator's `coder_out.files` use after
    serialization."""
    written: List[str] = []
    for f in files:
        path = f["path"]
        if path.startswith("/") or ".." in path.split("/"):
            logger.warning("write_files_to_tree: refusing suspicious path %s", path)
            continue
        full = REPO_ROOT / path
        # Defense in depth: verify the resolved path stays under REPO_ROOT.
        try:
            full.resolve().relative_to(REPO_ROOT.resolve())
        except ValueError:
            logger.warning("write_files_to_tree: refusing path outside repo: %s", path)
            continue
        new_content = f["new_content"]
        if full.exists():
            try:
                if full.read_text(encoding="utf-8", errors="replace") == new_content:
                    continue  # idempotent
            except Exception:
                pass
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(new_content)
        written.append(path)
    return written


# ── Smoke import (fast pre-pytest sanity) ───────────────────────────────────

def smoke_imports(module: str = "app.main") -> Tuple[bool, str]:
    """Fast pre-pytest check: can the entry-point module import?

    A failed top-level import shows up in pytest as 'collected 0 / N errors'
    which our regex parses as 0p/Nf — so the gate would mistakenly think
    the patch killed every test. The smoke check trips first and returns
    a clean error string before pytest runs.

    Bounded by 10s. Returns (ok, error_text_tail)."""
    try:
        proc = subprocess.run(
            [PYTHON, "-c", f"import {module}"],
            cwd=str(BACKEND_DIR),
            capture_output=True, text=True, timeout=SMOKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, "import smoke timeout"
    return proc.returncode == 0, (proc.stderr or proc.stdout or "")[-500:]


# ── pytest run ──────────────────────────────────────────────────────────────

_PASSED_RE = re.compile(r"(\d+)\s+passed")
_FAILED_RE = re.compile(r"(\d+)\s+failed")


_ERROR_RE = re.compile(r"(\d+)\s+error")

# pytest-cov prints a TOTAL line: "TOTAL   1234    456    63%". Capture the %.
_COV_TOTAL_RE = re.compile(r"TOTAL\s+\d+\s+\d+\s+(\d+)%")


def run_pytest(target: str = "tests/") -> Tuple[int, int, str]:
    """Run the backend test suite. Returns (passed, failed, summary_tail).

    `target` is interpreted relative to BACKEND_DIR. Uses -q + --tb=no for
    speed and to keep the summary parseable. -p no:cacheprovider stops
    pytest from scribbling .pytest_cache/ which was tripping our
    snapshot machinery.

    --continue-on-collection-errors keeps a broken test file (e.g. one
    that imports a deleted symbol) from blanking the whole run — the
    rest of the suite still executes and we get a real pass count to
    compare against. The collection error itself shows up in `failed`
    via pytest's error counter, which the orchestrator can use to
    decide whether the patch made the situation worse.

    Bounded by 90s. On timeout returns (0, 0, "pytest timeout") which the
    gate treats as a regression — better than letting an infinite-loop
    test hang the gate forever.
    """
    try:
        proc = subprocess.run(
            [PYTHON, "-m", "pytest", target,
             "-q", "--tb=no", "--no-header", "-p", "no:cacheprovider",
             "--continue-on-collection-errors"],
            cwd=str(BACKEND_DIR),
            capture_output=True, text=True, timeout=PYTEST_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return 0, 0, "pytest timeout"

    out = (proc.stdout or "") + (proc.stderr or "")
    passed = int(m.group(1)) if (m := _PASSED_RE.search(out)) else 0
    failed = int(m.group(1)) if (m := _FAILED_RE.search(out)) else 0
    # Collection errors aren't "failed tests" but they ARE regressions
    # we want the gate to notice — fold them into failed.
    errors = int(m.group(1)) if (m := _ERROR_RE.search(out)) else 0
    failed += errors
    return passed, failed, out[-2000:]


# ── Differential measurement (A2 — coverage deltas, not just pass/fail) ──────
#
# The regression-only pass-count gate is the right SAFETY signal, but it tells
# the loop NOTHING about whether a patch IMPROVED coverage — so the loop can't
# prefer the patch that adds a test over the one that merely doesn't regress.
# This adds a structured coverage measurement (overall % + passing test count)
# the loop can diff before/after a patch. It's OPT-IN (slower than the bare
# gate) — the synthesis pipeline (A1) and "did this test actually add coverage"
# checks use it; the hot per-actuate gate stays on the fast pass-count path.

class CoverageResult:
    """One coverage measurement: overall line-coverage %, passing test count,
    and the parsed failure count. -1 coverage_pct means 'could not measure'."""

    def __init__(self, coverage_pct: float, passed: int, failed: int, summary_tail: str):
        self.coverage_pct = coverage_pct
        self.passed = passed
        self.failed = failed
        self.summary_tail = summary_tail

    def __repr__(self) -> str:
        return (f"CoverageResult(cov={self.coverage_pct}%, passed={self.passed}, "
                f"failed={self.failed})")


def run_pytest_with_coverage(
    target: str = "tests/", cov_package: str = "app",
) -> CoverageResult:
    """Run the suite under coverage and parse the overall % + pass/fail counts.
    Slower than run_pytest (instrumentation overhead), so callers use it only
    when they need the coverage signal, not on every actuate. coverage_pct=-1.0
    when pytest-cov isn't available or the TOTAL line couldn't be parsed (the
    caller treats that as 'no signal', never as 0%)."""
    try:
        proc = subprocess.run(
            [PYTHON, "-m", "pytest", target,
             "-q", "--tb=no", "--no-header", "-p", "no:cacheprovider",
             "--continue-on-collection-errors",
             f"--cov={cov_package}", "--cov-report=term-missing:skip-covered"],
            cwd=str(BACKEND_DIR),
            capture_output=True, text=True, timeout=PYTEST_TIMEOUT_S * 2,
        )
    except subprocess.TimeoutExpired:
        return CoverageResult(-1.0, 0, 0, "pytest+cov timeout")
    except Exception as e:  # pragma: no cover - defensive
        return CoverageResult(-1.0, 0, 0, f"pytest+cov error: {e}")

    out = (proc.stdout or "") + (proc.stderr or "")
    passed = int(m.group(1)) if (m := _PASSED_RE.search(out)) else 0
    failed = int(m.group(1)) if (m := _FAILED_RE.search(out)) else 0
    failed += int(m.group(1)) if (m := _ERROR_RE.search(out)) else 0
    cov = float(m.group(1)) if (m := _COV_TOTAL_RE.search(out)) else -1.0
    return CoverageResult(cov, passed, failed, out[-2000:])


class CoverageDelta:
    """Structured before/after diff for a patch. `coverage_delta` > 0 means the
    patch raised line coverage; `tests_delta` > 0 means it added passing tests.
    `improved` is the loop-prioritization signal: a patch that adds coverage or
    tests without regressing is preferable to one that merely doesn't regress."""

    def __init__(self, before: CoverageResult, after: CoverageResult):
        self.before = before
        self.after = after
        self.coverage_delta = (
            round(after.coverage_pct - before.coverage_pct, 2)
            if before.coverage_pct >= 0 and after.coverage_pct >= 0 else 0.0
        )
        self.tests_delta = after.passed - before.passed
        self.regressed = after.passed < before.passed
        self.improved = (not self.regressed) and (
            self.coverage_delta > 0 or self.tests_delta > 0
        )

    def as_dict(self) -> Dict[str, object]:
        return {
            "coverage_before": self.before.coverage_pct,
            "coverage_after": self.after.coverage_pct,
            "coverage_delta": self.coverage_delta,
            "tests_before": self.before.passed,
            "tests_after": self.after.passed,
            "tests_delta": self.tests_delta,
            "regressed": self.regressed,
            "improved": self.improved,
        }

    def __repr__(self) -> str:
        return (f"CoverageDelta(cov {self.before.coverage_pct}%→{self.after.coverage_pct}% "
                f"Δ{self.coverage_delta:+}, tests {self.before.passed}→{self.after.passed} "
                f"Δ{self.tests_delta:+}, improved={self.improved})")


def measure_coverage_delta(
    files: List[Dict[str, str]], cov_package: str = "app",
) -> Tuple[CoverageDelta, Dict[str, Optional[str]]]:
    """Measure coverage BEFORE applying `files`, apply them, measure AFTER, and
    return (delta, snapshot). The CALLER restores the snapshot (same contract as
    gate_patch) — we don't auto-rollback. Use this to decide whether a patch
    (e.g. a synthesized test) actually ADDED coverage, not just whether it
    compiled. Best-effort: a measurement that can't run yields a neutral delta
    (improved=False) rather than raising."""
    paths = [f["path"] for f in files]
    snap = snapshot_files(paths)
    before = run_pytest_with_coverage(cov_package=cov_package)
    write_files_to_tree(files)
    after = run_pytest_with_coverage(cov_package=cov_package)
    return CoverageDelta(before, after), snap


# ── Baseline cache (cross-process via InflightRegistry.meta_kv) ─────────────

def baseline_pass_count() -> int:
    """Return the cached baseline pass count, computing it the first time.
    Stored in InflightRegistry's meta_kv so the CLI loop and the FastAPI
    process share one number — without that, both would race their own
    baselines and reject each other's wins."""
    cached = ir.meta_get(_BASELINE_KEY)
    if cached is not None:
        try:
            return int(cached)
        except ValueError:
            pass
    passed, _failed, _summary = run_pytest()
    ir.meta_set(_BASELINE_KEY, str(passed))
    return passed


def update_baseline(passing: int) -> None:
    """Update the cached baseline to a new value. Call after a successful
    apply that increased the pass count, so later actuates see the
    higher bar (preventing a regression-recovery from passing the gate
    by clearing tests that should already be green)."""
    ir.meta_set(_BASELINE_KEY, str(passing))


def reset_baseline() -> None:
    """Clear the cached baseline. Next call to baseline_pass_count will
    recompute. Useful for tests and after major branch switches."""
    ir.meta_set(_BASELINE_KEY, None)


# ── High-level gate (the one-call API the orchestrator uses) ────────────────

class GateResult:
    """Outcome of a single gate run. Keeps the orchestrator's call site terse."""

    def __init__(
        self,
        passed: bool,
        before: int,
        after: int,
        failed: int,
        reason: str,
        summary_tail: str,
    ) -> None:
        self.passed = passed
        self.before = before
        self.after = after
        self.failed = failed
        self.reason = reason
        self.summary_tail = summary_tail

    def __repr__(self) -> str:
        return (
            f"GateResult(passed={self.passed}, before={self.before}, "
            f"after={self.after}, failed={self.failed}, reason={self.reason!r})"
        )


def gate_patch(files: List[Dict[str, str]]) -> Tuple[GateResult, Dict[str, Optional[str]]]:
    """Apply `files` locally, run the gate (smoke + pytest), and report.
    Returns (result, snapshot). The CALLER is responsible for restoring
    the snapshot when result.passed is False — we don't auto-rollback
    so the orchestrator can keep the on-disk state if it wants to (e.g.
    to inspect what Coder produced for debugging).

    Two execution strategies, chosen at call time:

      • WORKTREE (default, when git is available): the gate runs in a throwaway
        `git worktree` checked out at HEAD — the real working tree is NEVER
        mutated, so a crash mid-gate can't leave the checkout dirty and two
        concurrent actuates can't race on the same files. We return an EMPTY
        snapshot in that case (nothing to restore — the real tree was untouched).

      • SNAPSHOT (fallback, when no git worktree / kill-switched off): the
        proven snapshot → apply → pytest → caller-restores path, returning the
        snapshot to restore.

    The orchestrator's call site is unchanged and correct under both:

        snap = ValidationGate.snapshot_files([f.path for f in files])
        try:
            result, _ = ValidationGate.gate_patch(serialized_files)
            if not result.passed:
                ValidationGate.restore_snapshot(snap)   # no-op when {} (worktree)
                return ActuateResponse(status='pytest_rejected', ...)
            ValidationGate.update_baseline(result.after)
            # ... proceed to branch + commit + PR ...
        except Exception:
            ValidationGate.restore_snapshot(snap)
            raise
    """
    started = time.time()
    paths = [f["path"] for f in files]

    # Prefer the worktree gate: it never touches the real tree. Pass our cached
    # baseline so it runs pytest exactly ONCE (it compares against before_count
    # instead of measuring a clean baseline in a second worktree). A None return
    # means no worktree could be created → fall through to the snapshot path.
    try:
        from app.services import sandbox
        if sandbox.worktree_gate_enabled():
            wt_result = sandbox.worktree_gate(
                files, ref="HEAD", before_count=baseline_pass_count(),
            )
            if wt_result is not None:
                logger.info(
                    "gate_patch: worktree strategy %s in %.1fs",
                    "ACCEPT" if wt_result.passed else "REJECT", time.time() - started,
                )
                # Empty snapshot: the real tree was never modified, so the
                # caller's restore_snapshot is a harmless no-op.
                return wt_result, {}
    except Exception as e:
        logger.warning(
            "gate_patch: worktree strategy errored (%s) — falling back to snapshot", e,
        )

    # ── Snapshot fallback (no git worktree available, or kill-switched off) ──
    snap = snapshot_files(paths)

    write_files_to_tree(files)

    # Phase 1 — smoke import. Catches catastrophic main.py breakage in <2s.
    smoke_ok, smoke_err = smoke_imports()
    if not smoke_ok:
        result = GateResult(
            passed=False,
            before=baseline_pass_count(),
            after=0, failed=0,
            reason=f"import smoke failed: {smoke_err[:200]}",
            summary_tail=smoke_err,
        )
        logger.warning("gate_patch: smoke import failed in %.1fs", time.time() - started)
        return result, snap

    # Phase 2 — pytest. Compare against baseline.
    before = baseline_pass_count()
    after, failed, summary = run_pytest()
    elapsed = time.time() - started

    # Treat pytest timeout as a regression (after=0, failed=0).
    if after == 0 and failed == 0 and "timeout" in summary:
        result = GateResult(
            passed=False, before=before, after=0, failed=0,
            reason="pytest timeout (>90s)",
            summary_tail=summary,
        )
        logger.warning("gate_patch: pytest timed out after %.1fs", elapsed)
        return result, snap

    if after < before:
        result = GateResult(
            passed=False, before=before, after=after, failed=failed,
            reason=f"pytest regression: {before} → {after} passing",
            summary_tail=summary,
        )
        logger.info("gate_patch: REJECT (%dp → %dp) in %.1fs", before, after, elapsed)
        return result, snap

    result = GateResult(
        passed=True, before=before, after=after, failed=failed,
        reason="ok",
        summary_tail=summary,
    )
    logger.info("gate_patch: ACCEPT (%dp → %dp, %df) in %.1fs",
                before, after, failed, elapsed)
    return result, snap


# ── Target-repo validation (clone + run THEIR tests) ─────────────────────────
#
# The gate above only ever tested ShipMate's OWN checkout (REPO_ROOT), so for
# every OTHER repo a user analyzes, "Build It" opened PRs with zero local
# validation. This validates the TARGET repo: shallow-clone it, apply the
# patch, detect + run ITS test command in a subprocess, parse pass/fail.
#
# ⚠️  SECURITY — this RUNS UNTRUSTED USER CODE (the cloned repo's test suite).
# It is therefore OFF by default and must be explicitly enabled with
# SHIPMATE_TARGET_REPO_GATE=1. Hardening applied:
#   • the test subprocess gets a STRIPPED environment (no backend secrets / .env
#     — only PATH/HOME/LANG + a flag), so a malicious test can't read our creds;
#   • everything is bounded (clone timeout, test timeout) and the temp workdir
#     is always removed in a finally;
#   • the clone uses --depth=1 --single-branch for speed.
# For real multi-tenant safety this should run in a container/VM; the env-strip
# + bounds here are the floor, not the ceiling. Document loudly when enabling.

_TARGET_REPO_GATE_ENABLED = os.getenv("SHIPMATE_TARGET_REPO_GATE", "0") == "1"
_CLONE_TIMEOUT_S = int(os.getenv("SHIPMATE_TARGET_CLONE_TIMEOUT_S", "60"))
_TARGET_TEST_TIMEOUT_S = int(os.getenv("SHIPMATE_TARGET_TEST_TIMEOUT_S", "180"))
_TARGET_TMP_PREFIX = "shipmate_target_"


def target_repo_gate_enabled() -> bool:
    """Read the flag at call time so tests/env changes take effect."""
    return os.getenv("SHIPMATE_TARGET_REPO_GATE", "0") == "1"


def detect_test_command(repo_path: str) -> Optional[List[str]]:
    """Best-effort detection of how to run THIS repo's tests, in priority order:
      1. Python: pytest.ini / pyproject.toml / setup.cfg / a tests dir → pytest
      2. Node:   package.json with a "test" script → npm test
      3. Make:   a Makefile with a `test:` target → make test
    Returns the argv list, or None if nothing recognizable is present (caller
    decides whether 'no tests' fails open or closed)."""
    root = Path(repo_path)

    # Python — pytest is the dominant runner.
    py_markers = ["pytest.ini", "tox.ini", "setup.cfg"]
    has_py_marker = any((root / m).exists() for m in py_markers)
    pyproject = root / "pyproject.toml"
    if pyproject.exists():
        try:
            txt = pyproject.read_text(encoding="utf-8", errors="replace")
            if "pytest" in txt or "[tool.poetry]" in txt or "[project]" in txt:
                has_py_marker = True
        except Exception:
            pass
    if not has_py_marker:
        for d in ("tests", "test"):
            if (root / d).is_dir():
                has_py_marker = True
                break
    if has_py_marker:
        return [sys.executable, "-m", "pytest", "-q", "--tb=no", "-p", "no:cacheprovider"]

    # Node — only if a real "test" script exists (skip the CRA placeholder that
    # exits 1 with no tests).
    pkg = root / "package.json"
    if pkg.exists():
        try:
            data = json.loads(pkg.read_text(encoding="utf-8", errors="replace"))
            scripts = data.get("scripts") or {}
            test_script = scripts.get("test", "")
            if test_script and "no test specified" not in test_script:
                return ["npm", "test", "--silent"]
        except Exception:
            pass

    # Make — a `test:` target.
    mk = root / "Makefile"
    if mk.exists():
        try:
            if re.search(r"^test\s*:", mk.read_text(encoding="utf-8", errors="replace"), re.MULTILINE):
                return ["make", "test"]
        except Exception:
            pass

    return None


def _safe_clone_url(owner: str, repo: str, access_token: Optional[str]) -> str:
    """HTTPS clone URL with the token embedded for private repos. The token is
    only ever passed to git here and never logged (we log the tokenless form)."""
    if access_token:
        return f"https://x-access-token:{access_token}@github.com/{owner}/{repo}.git"
    return f"https://github.com/{owner}/{repo}.git"


def _stripped_env(home_dir: Optional[str] = None) -> Dict[str, str]:
    """A minimal environment for the untrusted test subprocess.

    Omits everything from the backend's env (BEDROCK_*, GITHUB_*, AWS_*, the
    OAuth secret, …) so a hostile test script can't exfiltrate our secrets.

    CRITICAL: HOME is NOT inherited. The original kept the real HOME, so a
    hostile test could still `open(os.path.expanduser('~/.aws/credentials'))`
    and read the operator's cloud creds (and `~/.config/gh`, `~/.ssh`, `~/.netrc`)
    even though every secret ENV var was stripped. When `home_dir` is supplied
    (an ephemeral dir the caller creates and cleans up), HOME + the XDG dirs are
    redirected there, so `~` resolves into an empty directory. When None (legacy
    callers), HOME is simply absent — never the real one."""
    keep: Dict[str, str] = {}
    # HOME deliberately excluded from this inherit list — it's set below.
    # LD_LIBRARY_PATH / DYLD_LIBRARY_PATH are the dynamic-loader search paths for
    # OUR OWN interpreter — they are NOT secrets (they point at the Python
    # install dir, already discoverable via PATH/sys.executable). They MUST be
    # preserved: GitHub's setup-python toolcache interpreter is dynamically
    # linked against libpython in …/x64/lib and cannot even START without
    # LD_LIBRARY_PATH — stripping it made the nested `python -m pytest` exit 127
    # (loader failure) on the Linux runner before pytest ran, while passing on
    # macOS/Homebrew Python (lib path baked into rpath). Keeping them is safe and
    # necessary for the target-repo gate's subprocess to launch.
    for var in (
        "PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT",
        "LD_LIBRARY_PATH", "DYLD_LIBRARY_PATH",
    ):
        if var in os.environ:
            keep[var] = os.environ[var]
    if home_dir:
        keep["HOME"] = home_dir
        keep["XDG_CONFIG_HOME"] = os.path.join(home_dir, ".config")
        keep["XDG_CACHE_HOME"] = os.path.join(home_dir, ".cache")
    keep["SHIPMATE_SANDBOX"] = "1"   # marker tests can branch on if they want
    keep["CI"] = "true"
    return keep


def gate_patch_target_repo(
    owner: str, repo: str, branch: str,
    files: List[Dict[str, str]],
    access_token: Optional[str] = None,
    *,
    fail_open_when_no_tests: bool = True,
) -> GateResult:
    """Clone owner/repo@branch into an isolated tmp dir, apply `files`, detect +
    run the repo's own test command, and report. Always cleans up the workdir.

    Returns a GateResult. `before`/`after` are pass counts from the SINGLE post-
    patch run (there's no cheap baseline for an arbitrary repo, so the gate is
    'tests must not fail' rather than 'no regression'): passed=True iff the test
    command ran and reported 0 failures. When no test command is detected,
    `fail_open_when_no_tests` decides (default True → don't block a PR just
    because the repo has no tests; the reason makes that explicit)."""
    started = time.time()
    if not target_repo_gate_enabled():
        # Caller shouldn't reach here, but be safe: treat as skipped/pass.
        return GateResult(True, 0, 0, 0, "target-repo gate disabled", "")

    # A parent holds two SIBLINGS: the clone target (`workdir`, which git
    # requires to be empty) and an ephemeral HOME. Pointing HOME here means a
    # hostile test's `open('~/.aws/credentials')` resolves into an empty dir
    # instead of the operator's real creds. Both removed in finally.
    parent = tempfile.mkdtemp(prefix=_TARGET_TMP_PREFIX + uuid.uuid4().hex[:8] + "_")
    workdir = os.path.join(parent, "repo")
    home_dir = os.path.join(parent, "home")
    os.makedirs(home_dir, exist_ok=True)
    try:
        # 1. Shallow clone the branch.
        clone_url = _safe_clone_url(owner, repo, access_token)
        try:
            proc = subprocess.run(
                ["git", "clone", "--depth=1", "--single-branch",
                 "--branch", branch, clone_url, workdir],
                capture_output=True, text=True, timeout=_CLONE_TIMEOUT_S,
                env=_stripped_env(home_dir),
            )
        except subprocess.TimeoutExpired:
            return GateResult(False, 0, 0, 0,
                              f"clone timed out (>{_CLONE_TIMEOUT_S}s)", "")
        if proc.returncode != 0:
            # Don't leak the tokened URL in the error.
            tail = (proc.stderr or "")[-300:].replace(access_token or "\0", "***")
            return GateResult(False, 0, 0, 0,
                              f"clone failed for {owner}/{repo}@{branch}", tail)

        # 1b. SCRUB the clone credential. `git clone` persists the tokened
        #     remote URL into <workdir>/.git/config; the untrusted suite runs
        #     with that clone as cwd and could read the bearer token out of it.
        #     Rewrite the remote to the tokenless URL so there's no credential
        #     on disk for the suite to lift.
        try:
            subprocess.run(
                ["git", "-C", workdir, "remote", "set-url", "origin",
                 f"https://github.com/{owner}/{repo}.git"],
                capture_output=True, text=True, timeout=15, env=_stripped_env(home_dir),
            )
        except Exception as e:  # pragma: no cover - best-effort scrub
            logger.debug("could not scrub clone remote (%s)", e)

        # 2. Apply the patch files into the clone (reuse the same path-safety
        #    rules as the local gate, rooted at the clone dir).
        _apply_files_to_dir(workdir, files)

        # 3. Detect the test command.
        cmd = detect_test_command(workdir)
        if cmd is None:
            reason = "no test command detected in target repo"
            logger.warning("gate_patch_target_repo: %s (%s/%s)", reason, owner, repo)
            return GateResult(
                passed=fail_open_when_no_tests, before=0, after=0, failed=0,
                reason=reason + (" — passing (fail-open)" if fail_open_when_no_tests
                                 else " — blocking (fail-closed)"),
                summary_tail="",
            )

        # 4. Run the tests. The docker jail (--network none, read-only root,
        #    tmpfs HOME, resource caps) is the ONLY real containment for
        #    untrusted code, so it's REQUIRED by default. When docker is absent
        #    we FAIL CLOSED — we do NOT run a stranger's test suite as a bare
        #    host subprocess, because the env-strip + HOME-redirect floor does
        #    nothing against secrets at ABSOLUTE paths (e.g. this repo's own
        #    .env) and the host fallback has unrestricted network — a hostile
        #    conftest could read and exfiltrate them. The unsafe host path is
        #    available ONLY behind an explicit second opt-in for operators who
        #    accept the risk on an already-isolated host.
        try:
            from app.services import sandbox
            use_docker = sandbox.docker_available()
        except Exception:
            use_docker = False

        allow_host = os.getenv("SHIPMATE_ALLOW_UNSANDBOXED_TARGET_TESTS", "0") == "1"
        if not use_docker and not allow_host:
            reason = (
                "docker unavailable — refusing to run untrusted target-repo tests "
                "on the host without isolation (set SHIPMATE_ALLOW_UNSANDBOXED_TARGET_TESTS=1 "
                "ONLY on an already-isolated host to override). Gate skipped."
            )
            logger.warning("gate_patch_target_repo: %s (%s/%s)", reason, owner, repo)
            # Fail OPEN on the verdict (don't block the PR) but DON'T execute
            # untrusted code: the patch simply ships without target-repo test
            # validation, which is the pre-Phase-2 behaviour for docker-less hosts.
            return GateResult(True, 0, 0, 0, reason, "")

        try:
            if use_docker:
                from app.services import sandbox
                argv = sandbox.build_docker_argv(workdir, cmd)
                logger.info("gate_patch_target_repo: running tests in docker jail")
                proc = subprocess.run(
                    argv, capture_output=True, text=True,
                    timeout=_TARGET_TEST_TIMEOUT_S,
                )
            else:
                # Explicitly opted into the unsandboxed host path (allow_host).
                logger.warning(
                    "gate_patch_target_repo: running UNSANDBOXED on host "
                    "(SHIPMATE_ALLOW_UNSANDBOXED_TARGET_TESTS=1) — operator-accepted risk",
                )
                proc = subprocess.run(
                    cmd, cwd=workdir, capture_output=True, text=True,
                    timeout=_TARGET_TEST_TIMEOUT_S, env=_stripped_env(home_dir),
                )
        except subprocess.TimeoutExpired:
            return GateResult(False, 0, 0, 0,
                              f"target tests timed out (>{_TARGET_TEST_TIMEOUT_S}s)", "")
        except FileNotFoundError as e:
            # Runner not installed on the host (e.g. npm/make missing).
            return GateResult(True, 0, 0, 0,
                              f"test runner unavailable ({e}); skipping gate", "")

        out = (proc.stdout or "") + (proc.stderr or "")
        passed = int(m.group(1)) if (m := _PASSED_RE.search(out)) else 0
        failed = int(m.group(1)) if (m := _FAILED_RE.search(out)) else 0
        errors = int(m.group(1)) if (m := _ERROR_RE.search(out)) else 0
        failed += errors
        elapsed = time.time() - started

        # 'tests must not fail' — a non-zero exit with parsed failures rejects.
        ok = failed == 0 and proc.returncode == 0
        if ok:
            reason = "ok"
        else:
            # Include a stderr tail on failure — a bare 'exit=127' is
            # undebuggable (was it a loader failure? missing runner? import
            # crash?). The parsed counters miss a process that died before
            # emitting a pytest summary line, so surface the raw tail.
            err_tail = (proc.stderr or "")[-600:].strip()
            reason = f"target tests failed ({failed} failing, exit={proc.returncode})"
            if proc.returncode != 0 and err_tail:
                reason += f" | stderr: {err_tail}"
        logger.info("gate_patch_target_repo: %s/%s %s (%dp/%df) in %.1fs",
                    owner, repo, "ACCEPT" if ok else "REJECT", passed, failed, elapsed)
        return GateResult(ok, before=passed, after=passed, failed=failed,
                          reason=reason, summary_tail=out[-2000:])
    finally:
        # Remove the whole parent (clone + ephemeral HOME) in one shot.
        shutil.rmtree(parent, ignore_errors=True)


def _apply_files_to_dir(base_dir: str, files: List[Dict[str, str]]) -> List[str]:
    """Write Coder output files under `base_dir`, with the same path-traversal
    guards as write_files_to_tree but rooted at an arbitrary directory (the
    clone), not REPO_ROOT."""
    root = Path(base_dir).resolve()
    written: List[str] = []
    for f in files:
        path = f["path"]
        if path.startswith("/") or ".." in path.split("/"):
            logger.warning("_apply_files_to_dir: refusing suspicious path %s", path)
            continue
        full = root / path
        try:
            full.resolve().relative_to(root)
        except ValueError:
            logger.warning("_apply_files_to_dir: refusing path outside clone: %s", path)
            continue
        full.parent.mkdir(parents=True, exist_ok=True)
        full.write_text(f["new_content"])
        written.append(path)
    return written
