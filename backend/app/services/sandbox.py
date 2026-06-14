"""
Sandbox — isolated execution for the validation gate (Phase 2).

Two problems with the original snapshot/restore gate this module fixes:

  1. It MUTATED the real working tree (write files → run pytest → restore). A
     crash between write and restore left ShipMate's own checkout dirty, and two
     concurrent actuates raced on the same files. `worktree_gate` runs the gate
     in a throwaway `git worktree` checked out at a committed ref — the real
     tree is never touched, so it's crash-safe and concurrency-safe by
     construction (each call gets its own worktree dir).

  2. The untrusted target-repo path ran a stranger's test suite in a subprocess
     that still inherited the real HOME — so a hostile test could read
     `~/.aws/credentials`, `~/.config/gh/hosts.yml`, `~/.ssh`, etc. even though
     the rest of the env was stripped. `stripped_env` now points HOME at an
     EPHEMERAL dir inside the throwaway workdir, and `docker_gate` runs the
     suite in a container with `--network none`, a read-only root, tmpfs HOME,
     and memory/cpu/pid caps — the real jail the env-strip only gestured at.

Plus `gate_for(kind, category)` — a per-finding tier so a docs tweak doesn't pay
for a full pytest run while a security patch does (consumed by the gate today
and the inner verify-loop in Phase 4).

Everything is fail-open: a sandbox that can't be set up (no git worktree, no
docker) returns a sentinel the caller treats as "fall back to the proven path,"
never an exception that breaks an actuate.
"""
from __future__ import annotations

import logging
import os
import shutil
import subprocess
import tempfile
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

# Leaf utilities shared with the snapshot path. validation_gate imports THIS
# module lazily (inside its function bodies) to avoid an import cycle, so by the
# time sandbox is first imported, validation_gate is fully loaded.
from app.services.validation_gate import (
    GateResult,
    PYTHON,
    REPO_ROOT,
    PYTEST_TIMEOUT_S,
    SMOKE_TIMEOUT_S,
    _apply_files_to_dir,
    _ERROR_RE,
    _FAILED_RE,
    _PASSED_RE,
)

logger = logging.getLogger("shipmate.sandbox")

_WORKTREE_TMP_PREFIX = "shipmate_wt_"
# Kill-switch: SHIPMATE_WORKTREE_GATE=0 forces the legacy snapshot path even
# when a worktree could be created. Default on (empty/anything-but-0 ⇒ enabled).
_WORKTREE_DEFAULT = "1"

# Docker resource caps for the untrusted target-repo jail. Conservative — a test
# suite that needs more than this is an outlier we'd rather reject than host.
_DOCKER_MEMORY = os.getenv("SHIPMATE_DOCKER_MEMORY", "1g")
_DOCKER_CPUS = os.getenv("SHIPMATE_DOCKER_CPUS", "2")
_DOCKER_PIDS = os.getenv("SHIPMATE_DOCKER_PIDS", "512")
_DOCKER_IMAGE = os.getenv("SHIPMATE_DOCKER_IMAGE", "python:3.12-slim")


# ── Stripped / ephemeral-HOME environment ────────────────────────────────────

def stripped_env(home_dir: str) -> Dict[str, str]:
    """A minimal environment for an untrusted subprocess.

    Deliberately omits everything from the backend's env (BEDROCK_*, GITHUB_*,
    AWS_*, the OAuth secret, …) AND points HOME at `home_dir` — an EPHEMERAL
    directory the caller created inside its throwaway workdir. That's the fix
    for the real hole: with HOME redirected, `os.path.expanduser('~/.aws')`,
    `~/.config/gh`, `~/.ssh`, `~/.netrc` all resolve into an empty dir, so a
    hostile test script can't read the operator's credentials even though the
    process still runs on the host.

    `home_dir` MUST be created by the caller (and cleaned up with the workdir).
    """
    keep: Dict[str, str] = {}
    # Note: HOME is intentionally NOT copied from os.environ — it's overridden.
    for var in ("PATH", "LANG", "LC_ALL", "TMPDIR", "SYSTEMROOT"):
        if var in os.environ:
            keep[var] = os.environ[var]
    keep["HOME"] = home_dir
    keep["XDG_CONFIG_HOME"] = os.path.join(home_dir, ".config")
    keep["XDG_CACHE_HOME"] = os.path.join(home_dir, ".cache")
    keep["SHIPMATE_SANDBOX"] = "1"   # marker tests can branch on if they want
    keep["CI"] = "true"
    return keep


# ── pytest parsing (shared shape with validation_gate) ───────────────────────

def _parse_pytest(out: str) -> Tuple[int, int]:
    """(passed, failed+errors) from a pytest -q tail. Collection errors fold
    into failed — they're regressions we want the gate to notice."""
    passed = int(m.group(1)) if (m := _PASSED_RE.search(out)) else 0
    failed = int(m.group(1)) if (m := _FAILED_RE.search(out)) else 0
    failed += int(m.group(1)) if (m := _ERROR_RE.search(out)) else 0
    return passed, failed


# ── git-worktree gate ─────────────────────────────────────────────────────────

def worktree_available(repo_root: Optional[Path] = None) -> bool:
    """True when `repo_root` is inside a git work tree and the git CLI is
    present — i.e. a worktree gate can actually be created."""
    root = repo_root or REPO_ROOT
    if shutil.which("git") is None:
        return False
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "rev-parse", "--is-inside-work-tree"],
            capture_output=True, text=True, timeout=10,
        )
    except Exception:
        return False
    return proc.returncode == 0 and proc.stdout.strip() == "true"


def worktree_gate_enabled() -> bool:
    """Read the kill-switch at call time so tests/env changes take effect.
    Default ON; set SHIPMATE_WORKTREE_GATE=0 to force the legacy snapshot path."""
    return os.getenv("SHIPMATE_WORKTREE_GATE", _WORKTREE_DEFAULT) != "0"


def working_tree_clean(repo_root: Optional[Path] = None) -> bool:
    """True when no TRACKED file differs from HEAD. The worktree gate checks out
    HEAD; if the working tree has uncommitted modifications, validating against
    HEAD would (a) compare a working-tree baseline to a HEAD+patch run and (b)
    apply the patch on top of stale code — both wrong. So the gate only uses a
    worktree when the tree is clean (then HEAD == working tree and the baseline
    is consistent); a dirty tree falls back to the snapshot path, which operates
    on the real tree and is correct there. Untracked files are ignored (a new
    file the Coder creates isn't in HEAD or the tree yet — harmless)."""
    root = repo_root or REPO_ROOT
    try:
        proc = subprocess.run(
            ["git", "-C", str(root), "diff", "--name-only", "HEAD"],
            capture_output=True, text=True, timeout=15,
        )
    except Exception:
        return False  # can't tell ⇒ assume dirty ⇒ snapshot fallback (safe)
    if proc.returncode != 0:
        return False
    return proc.stdout.strip() == ""


def default_pytest_runner(target: str = "tests/") -> Callable[[Path], Tuple[int, int, str]]:
    """Build the standard backend-pytest runner used inside a worktree. Returns
    a callable (worktree_root) -> (passed, failed, tail). Runs from the
    worktree's `backend/` dir with the REAL venv interpreter (sys.executable is
    absolute), so code is isolated to the worktree while deps come from the
    installed venv."""
    def _run(worktree_root: Path) -> Tuple[int, int, str]:
        backend = worktree_root / "backend"
        # The worktree's pytest run gets its OWN environment so it can't touch
        # the live backend's state:
        #   • SHIPMATE_STORE_DIR → an isolated temp dir, so the suite's
        #     conftest store-reset wipes a throwaway db, NOT the live
        #     finding_journal / inflight / baseline the running backend uses.
        #   • SHIPMATE_WORKTREE_GATE=0 inside the run, so any gate-exercising
        #     test (test_sandbox.py) uses the snapshot path instead of spawning
        #     MORE nested worktrees off this one.
        env = dict(os.environ)
        store_dir = tempfile.mkdtemp(prefix="shipmate_wt_store_")
        env["SHIPMATE_STORE_DIR"] = store_dir
        env["SHIPMATE_WORKTREE_GATE"] = "0"
        try:
            proc = subprocess.run(
                [PYTHON, "-m", "pytest", target,
                 "-q", "--tb=no", "--no-header", "-p", "no:cacheprovider",
                 "--continue-on-collection-errors"],
                cwd=str(backend), capture_output=True, text=True,
                timeout=PYTEST_TIMEOUT_S, env=env,
            )
        except subprocess.TimeoutExpired:
            return 0, 0, "pytest timeout"
        finally:
            shutil.rmtree(store_dir, ignore_errors=True)
        out = (proc.stdout or "") + (proc.stderr or "")
        passed, failed = _parse_pytest(out)
        return passed, failed, out[-2000:]
    return _run


def _smoke_in_worktree(worktree_root: Path, module: str = "app.main") -> Tuple[bool, str]:
    """Can the entry-point module import inside the worktree? Fast pre-pytest
    sanity (a broken top-level import otherwise shows up as 0 collected → the
    gate would think the patch killed every test)."""
    backend = worktree_root / "backend"
    try:
        proc = subprocess.run(
            [PYTHON, "-c", f"import {module}"],
            cwd=str(backend), capture_output=True, text=True, timeout=SMOKE_TIMEOUT_S,
        )
    except subprocess.TimeoutExpired:
        return False, "import smoke timeout"
    return proc.returncode == 0, (proc.stderr or proc.stdout or "")[-500:]


@contextmanager
def _worktree(ref: str, root: Path):
    """Context manager yielding a throwaway `git worktree` path checked out at
    `ref`, ALWAYS torn down on exit (crash-safe). Yields None when a worktree
    can't be created so callers fall back. Centralizes the add/teardown so
    worktree_gate and worktree_smoke share one correct lifecycle.

    Teardown order matters: `worktree remove` can fail (locked/busy), leaving
    both the dir AND git's registration; a bare `prune` won't reclaim a
    registration whose dir still exists. So we always rmtree the parent (can't
    leak the checkout) BEFORE prune, making a stale registration reclaimable."""
    wt_parent = tempfile.mkdtemp(prefix=_WORKTREE_TMP_PREFIX + uuid.uuid4().hex[:8] + "_")
    wt_path = Path(wt_parent) / "tree"   # git creates this leaf dir
    created = False
    try:
        try:
            proc = subprocess.run(
                ["git", "-C", str(root), "worktree", "add", "--detach",
                 "--force", str(wt_path), ref],
                capture_output=True, text=True, timeout=60,
            )
        except Exception as e:
            logger.warning("worktree add failed to spawn (%s) — caller falls back", e)
            yield None
            return
        if proc.returncode != 0:
            logger.warning(
                "worktree add returned %d (%s) — caller falls back",
                proc.returncode, (proc.stderr or "")[-200:],
            )
            yield None
            return
        created = True
        yield wt_path
    finally:
        if created:
            removed = False
            try:
                rm = subprocess.run(
                    ["git", "-C", str(root), "worktree", "remove", "--force", str(wt_path)],
                    capture_output=True, text=True, timeout=30,
                )
                removed = rm.returncode == 0
            except Exception as e:
                logger.debug("worktree remove failed (%s)", e)
            shutil.rmtree(wt_parent, ignore_errors=True)
            if not removed:
                logger.warning(
                    "worktree remove did not succeed for %s — pruning stale registration",
                    wt_path,
                )
            try:
                subprocess.run(
                    ["git", "-C", str(root), "worktree", "prune"],
                    capture_output=True, text=True, timeout=30,
                )
            except Exception:
                pass
        else:
            shutil.rmtree(wt_parent, ignore_errors=True)


def worktree_smoke(
    files: List[Dict[str, str]],
    ref: str = "HEAD",
    *,
    module: str = "app.main",
    repo_root: Optional[Path] = None,
) -> Optional[Tuple[bool, str]]:
    """Apply `files` in a throwaway worktree and run ONLY the import smoke —
    race-free and crash-safe, the real tree untouched. Returns (ok, detail), or
    None when no worktree could be created (caller falls back to a snapshot
    smoke or skips). This is the EXECUTION step the inner verify-loop uses: it's
    cheap (~2s) and safe to run before path claims, unlike a snapshot smoke that
    mutates the shared tree."""
    root = repo_root or REPO_ROOT
    if not worktree_available(root):
        return None
    with _worktree(ref, root) as wt_path:
        if wt_path is None:
            return None
        _apply_files_to_dir(str(wt_path), files)
        return _smoke_in_worktree(wt_path, module)


def worktree_gate(
    files: List[Dict[str, str]],
    ref: str = "HEAD",
    *,
    before_count: Optional[int] = None,
    runner: Optional[Callable[[Path], Tuple[int, int, str]]] = None,
    smoke: bool = True,
    repo_root: Optional[Path] = None,
) -> Optional[GateResult]:
    """Run the validation gate in a throwaway `git worktree` checked out at
    `ref`, so the real working tree is NEVER mutated.

    Returns a GateResult, or None when a worktree can't be created (caller falls
    back to the snapshot path). The worktree is always removed in `finally` —
    crash-safe — and `git worktree prune` mops up any stale registration.

    Args:
        files:        [{path, new_content}] — repo-relative, applied into the WT.
        ref:          commit/branch the worktree is based on (default HEAD).
        before_count: baseline passing count to compare against. When None, the
                      gate measures the baseline IN the clean worktree first
                      (correct but 2× pytest); the orchestrator passes its
                      cached baseline_pass_count() to keep cost at 1× pytest.
        runner:       (worktree_root)->(passed, failed, tail). Defaults to the
                      standard backend pytest. Injected by tests for speed.
        smoke:        run the import smoke check before pytest (default True).
    """
    root = repo_root or REPO_ROOT
    if not worktree_available(root):
        return None

    # A worktree checks out HEAD. If the real tree has uncommitted TRACKED
    # changes, validating against HEAD would both apply the patch on stale code
    # and compare a working-tree baseline to a HEAD run — wrong on both counts.
    # Fall back to the snapshot path (correct for a dirty tree). The injected-
    # runner test path skips this (a test supplies before_count + runner and is
    # asserting mechanics, not baseline consistency).
    if runner is None and not working_tree_clean(root):
        logger.info("worktree_gate: working tree dirty — falling back to snapshot path")
        return None

    run = runner or default_pytest_runner()
    started = time.time()
    with _worktree(ref, root) as wt_path:
        if wt_path is None:
            return None  # couldn't create a worktree → caller falls back

        # Apply the patch into the worktree (path-safe, rooted at the WT).
        _apply_files_to_dir(str(wt_path), files)

        # Smoke import first — catches catastrophic entry-point breakage in <2s.
        if smoke:
            ok, smoke_err = _smoke_in_worktree(wt_path)
            if not ok:
                base = before_count if before_count is not None else 0
                return GateResult(
                    passed=False, before=base, after=0, failed=0,
                    reason=f"import smoke failed: {smoke_err[:200]}",
                    summary_tail=smoke_err,
                )

        # Baseline: measure in the clean worktree only if the caller didn't
        # supply one (the orchestrator passes its cached baseline → 1× pytest).
        before = before_count
        if before is None:
            # The self-measuring path would need a second worktree; callers
            # should pass before_count. Fall back to 0 ('tests must not fail').
            before = 0

        after, failed, summary = run(wt_path)
        elapsed = time.time() - started

        if after == 0 and failed == 0 and "timeout" in summary:
            return GateResult(
                passed=False, before=before, after=0, failed=0,
                reason=f"pytest timeout (>{PYTEST_TIMEOUT_S}s)", summary_tail=summary,
            )
        if after < before:
            logger.info("worktree_gate: REJECT (%dp → %dp) in %.1fs", before, after, elapsed)
            return GateResult(
                passed=False, before=before, after=after, failed=failed,
                reason=f"pytest regression: {before} → {after} passing",
                summary_tail=summary,
            )
        logger.info("worktree_gate: ACCEPT (%dp → %dp, %df) in %.1fs",
                    before, after, failed, elapsed)
        return GateResult(
            passed=True, before=before, after=after, failed=failed,
            reason="ok", summary_tail=summary,
        )


# ── Docker jail for the untrusted target-repo gate ───────────────────────────

def docker_available() -> bool:
    """True when the docker CLI is present AND the daemon answers. Cached per
    process so we don't shell out on every actuate."""
    global _DOCKER_AVAILABLE
    if _DOCKER_AVAILABLE is not None:
        return _DOCKER_AVAILABLE
    if shutil.which("docker") is None:
        _DOCKER_AVAILABLE = False
        return False
    try:
        proc = subprocess.run(
            ["docker", "info", "--format", "{{.ServerVersion}}"],
            capture_output=True, text=True, timeout=15,
        )
        _DOCKER_AVAILABLE = proc.returncode == 0
    except Exception:
        _DOCKER_AVAILABLE = False
    return _DOCKER_AVAILABLE


_DOCKER_AVAILABLE: Optional[bool] = None


def reset_docker_cache() -> None:
    """Test hook: clear the cached docker-availability probe."""
    global _DOCKER_AVAILABLE
    _DOCKER_AVAILABLE = None


def build_docker_argv(
    host_dir: str,
    inner_cmd: List[str],
    *,
    image: str = _DOCKER_IMAGE,
    memory: str = _DOCKER_MEMORY,
    cpus: str = _DOCKER_CPUS,
    pids: str = _DOCKER_PIDS,
) -> List[str]:
    """Build the hardened `docker run` argv that executes `inner_cmd` against the
    code mounted (read-only) at `host_dir`. Pure — no docker needed — so the
    jail's safety properties are unit-testable.

    Hardening:
      • --network none         no exfiltration / no calling home.
      • --read-only root       the container can't persist or tamper with itself.
      • --tmpfs /tmp, HOME      writable scratch ONLY in memory; HOME is an empty
                                tmpfs so ~/.aws etc. don't exist in the container.
      • --memory/--cpus/--pids  bound resource use so a fork-bomb / OOM test
                                can't take the host down.
      • --cap-drop ALL, no-new-privileges  minimal capabilities.
      • code mounted :ro at /work; the suite runs from a tmpfs copy it makes, or
        read-only in place (tests that need to write artifacts use /tmp).

    Interpreter normalization: callers build the inner command with
    `sys.executable` (e.g. `[sys.executable, "-m", "pytest", …]`) so the HOST
    path works. But that absolute path (e.g. GitHub's
    /opt/hostedtoolcache/Python/3.11/x64/bin/python) does NOT exist inside the
    container image, which has its own `python`. Passing the host path made
    `docker run` exit 127 ("no such file or directory") before pytest ran. So a
    leading host-Python interpreter token is rewritten to the image's `python`.
    """
    return [
        "docker", "run", "--rm",
        "--network", "none",
        "--read-only",
        "--tmpfs", "/tmp:rw,size=256m",
        "--tmpfs", "/sandbox-home:rw,size=64m",
        "--env", "HOME=/sandbox-home",
        "--env", "CI=true",
        "--env", "SHIPMATE_SANDBOX=1",
        "--memory", memory,
        "--cpus", cpus,
        "--pids-limit", pids,
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        "--workdir", "/work",
        "--volume", f"{host_dir}:/work:ro",
        image,
        *_containerize_cmd(inner_cmd),
    ]


def _containerize_cmd(inner_cmd: List[str]) -> List[str]:
    """Rewrite a leading HOST python-interpreter path to the container's
    `python`. The host's sys.executable (an absolute toolcache/venv path) is not
    present in the image; the image has `python` on PATH. Only the interpreter
    token is touched — `-m pytest …` and everything else is passed through. A
    non-python command (npm test, make test) is returned unchanged."""
    if not inner_cmd:
        return inner_cmd
    first = inner_cmd[0]
    base = os.path.basename(first)
    # Absolute/relative path to a python interpreter → use the image's `python`.
    if ("/" in first or first.endswith((".exe",))) and base.lower().startswith("python"):
        return ["python", *inner_cmd[1:]]
    return list(inner_cmd)


# ── Per-finding gate tiers ───────────────────────────────────────────────────

@dataclass(frozen=True)
class GateTier:
    """What validation a finding warrants. Lets a docs tweak skip a full pytest
    run while a security patch pays for it. `pytest_target` is relative to the
    backend dir; empty when run_pytest is False."""
    name: str
    run_smoke: bool
    run_pytest: bool
    pytest_target: str
    description: str


# Canonical tiers.
_TIER_LINT = GateTier(
    "lint", run_smoke=False, run_pytest=False, pytest_target="",
    description="parse/lint only — no behavior to exercise (docs, comments)",
)
_TIER_IMPORT_SMOKE = GateTier(
    "import-smoke", run_smoke=True, run_pytest=False, pytest_target="",
    description="entry point must still import — for manifest/dependency edits",
)
_TIER_TEST = GateTier(
    "test", run_smoke=True, run_pytest=True, pytest_target="tests/",
    description="collect + run the suite — for test additions",
)
_TIER_FULL = GateTier(
    "full", run_smoke=True, run_pytest=True, pytest_target="tests/",
    description="smoke + full pytest — security/feature/behavioral changes",
)

# Categories that imply a manifest/dependency edit (import-smoke is enough).
_MANIFEST_CATEGORIES = frozenset({"deps", "dependency", "dependencies"})
# Categories/kinds that are pure prose (lint-only).
_DOCS_CATEGORIES = frozenset({"docs", "documentation", "readme"})

# gate_kind string (from a Skill) -> the concrete GateTier to run. Single point
# where the skill's declared validation tier becomes a runnable tier.
_GATE_KIND_TO_TIER = {
    "lint": _TIER_LINT,
    "import-smoke": _TIER_IMPORT_SMOKE,
    "test": _TIER_TEST,
    "full": _TIER_FULL,
}


def gate_for(kind: str, category: str = "") -> GateTier:
    """Pick the validation tier for a finding. Deterministic (no LLM).

    The skill registry (skills.skill_for) is the SINGLE source of truth for the
    kind/category → tier mapping: each skill declares its `gate_kind` next to
    its behaviour rules, and we translate that to a runnable GateTier here. Two
    gate-ONLY distinctions have no dedicated skill (a docs or manifest edit
    still gets feature/base prompt rules) but warrant a cheaper tier, so they're
    special-cased first:
      • docs/readme   → lint only (pure prose, nothing to execute)
      • deps/manifest → import-smoke (entry point must still import)
    Everything else inherits the matched skill's gate_kind (e.g. write-test →
    test, fix-ci → lint, fix-security/add-feature/refactor/fix-bug/unknown →
    full). Fail-safe: any lookup miss → full (over-validate, never under).
    """
    k = (kind or "").lower().strip()
    c = (category or "").lower().strip()

    if c in _DOCS_CATEGORIES or k in _DOCS_CATEGORIES:
        return _TIER_LINT
    if c in _MANIFEST_CATEGORIES:
        return _TIER_IMPORT_SMOKE

    try:
        from app.services import skills
        gate_kind = skills.skill_for(k, c).gate_kind
        return _GATE_KIND_TO_TIER.get(gate_kind, _TIER_FULL)
    except Exception:  # pragma: no cover - fail-safe to full
        return _TIER_FULL
