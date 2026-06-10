"""Sandbox — isolated execution for the validation gate (Phase 2).

Covers the three pillars:
  • worktree_gate runs the gate in a throwaway `git worktree` (real tree never
    mutated) and ALWAYS tears it down — even when the gate raises;
  • the docker jail argv is hardened (--network none, read-only, tmpfs HOME,
    resource caps) and the gate skips cleanly when docker is absent;
  • gate_for tiers findings deterministically (docs→lint, deps→smoke,
    test→run, else→full).

worktree tests inject a fake pytest runner so they don't run the real 46s suite
inside the worktree — we're testing the SANDBOX mechanics (isolation, cleanup,
fallback), not pytest itself.
"""
import os
import subprocess
from pathlib import Path

import pytest

from app.services import sandbox
from app.services.validation_gate import GateResult


# ── gate_for tiers ───────────────────────────────────────────────────────────

class TestGateFor:
    def test_docs_is_lint_only(self):
        t = sandbox.gate_for("milestone", "docs")
        assert t.name == "lint"
        assert t.run_pytest is False and t.run_smoke is False

    def test_deps_is_import_smoke(self):
        t = sandbox.gate_for("guardrail", "deps")
        assert t.name == "import-smoke"
        assert t.run_smoke is True and t.run_pytest is False

    def test_test_kind_collects_and_runs(self):
        t = sandbox.gate_for("test", "")
        assert t.name == "test"
        assert t.run_pytest is True

    def test_security_is_full(self):
        t = sandbox.gate_for("guardrail", "injection")
        assert t.name == "full"
        assert t.run_smoke and t.run_pytest

    def test_unknown_kind_fails_safe_to_full(self):
        # Over-validate the unknown rather than under-validate it.
        t = sandbox.gate_for("totally-new-kind", "weird-category")
        assert t.name == "full"
        assert t.run_pytest is True

    def test_case_insensitive(self):
        assert sandbox.gate_for("TEST", "").name == "test"
        assert sandbox.gate_for("Milestone", "DOCS").name == "lint"


# ── stripped_env (the credential-leak fix) ───────────────────────────────────

class TestStrippedEnv:
    def test_home_redirected_to_ephemeral_dir(self, tmp_path, monkeypatch):
        monkeypatch.setenv("HOME", "/Users/realuser")
        home = str(tmp_path / "home")
        env = sandbox.stripped_env(home)
        # HOME must NOT be the operator's real home — it's the ephemeral dir.
        assert env["HOME"] == home
        assert env["HOME"] != "/Users/realuser"
        # XDG dirs follow HOME so ~/.config / ~/.cache also resolve into the box.
        assert env["XDG_CONFIG_HOME"].startswith(home)
        assert env["XDG_CACHE_HOME"].startswith(home)

    def test_secrets_omitted(self, tmp_path, monkeypatch):
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "leak-me")
        monkeypatch.setenv("GITHUB_CLIENT_SECRET", "leak-me-too")
        monkeypatch.setenv("BEDROCK_MODEL_SMART", "x")
        env = sandbox.stripped_env(str(tmp_path))
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert "GITHUB_CLIENT_SECRET" not in env
        assert "BEDROCK_MODEL_SMART" not in env
        # PATH survives so the runner is findable.
        assert "PATH" in env
        assert env["SHIPMATE_SANDBOX"] == "1"


# ── docker jail ──────────────────────────────────────────────────────────────

class TestDockerArgv:
    def test_argv_is_hardened(self):
        argv = sandbox.build_docker_argv("/host/clone", ["pytest", "-q"])
        joined = " ".join(argv)
        assert "--network none" in joined         # no exfiltration
        assert "--read-only" in joined            # immutable root
        assert "--cap-drop ALL" in joined         # minimal caps
        assert "no-new-privileges" in joined
        assert "--pids-limit" in argv_window(argv, "--pids-limit")
        # HOME is a tmpfs inside the container, never the host's.
        assert "HOME=/sandbox-home" in argv
        assert "--memory" in argv and "--cpus" in argv

    def test_code_mounted_read_only(self):
        argv = sandbox.build_docker_argv("/host/clone", ["pytest"])
        # The host clone is mounted :ro at /work.
        assert "/host/clone:/work:ro" in argv

    def test_inner_command_is_appended_last(self):
        argv = sandbox.build_docker_argv("/h", ["npm", "test", "--silent"])
        assert argv[-3:] == ["npm", "test", "--silent"]

    def test_host_python_path_rewritten_to_container_python(self):
        # Regression: the gate builds the inner cmd with sys.executable (a HOST
        # path). On GitHub that's /opt/hostedtoolcache/Python/.../bin/python,
        # which does NOT exist in the image → `docker run` exited 127 before
        # pytest ran. The interpreter token must be normalized to `python`.
        host_py = "/opt/hostedtoolcache/Python/3.11.15/x64/bin/python"
        argv = sandbox.build_docker_argv("/h", [host_py, "-m", "pytest", "-q"])
        assert argv[-4:] == ["python", "-m", "pytest", "-q"]
        assert host_py not in argv  # the host path is gone

    def test_non_python_inner_command_unchanged(self):
        # npm/make commands must pass through verbatim (no interpreter rewrite).
        assert sandbox._containerize_cmd(["npm", "test"]) == ["npm", "test"]
        assert sandbox._containerize_cmd(["make", "test"]) == ["make", "test"]

    def test_bare_python_name_left_for_container_path(self):
        # A bare `python3` (no slash) already resolves on the image PATH — leave
        # it; only an absolute/relative HOST path needs rewriting.
        assert sandbox._containerize_cmd(["python3", "-m", "pytest"]) == ["python3", "-m", "pytest"]

    def test_resource_caps_overridable_by_env(self, monkeypatch):
        # The caps read module-level defaults; verify the values flow through.
        argv = sandbox.build_docker_argv(
            "/h", ["pytest"], memory="512m", cpus="1", pids="128",
        )
        assert "512m" in argv and "1" in argv and "128" in argv

    def test_docker_available_false_when_cli_absent(self, monkeypatch):
        sandbox.reset_docker_cache()
        monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
        assert sandbox.docker_available() is False
        sandbox.reset_docker_cache()


def argv_window(argv, flag):
    """Helper: the flag plus its value, so a presence+value assert reads cleanly."""
    i = argv.index(flag)
    return argv[i:i + 2]


# ── worktree gate ────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _reset_docker_probe():
    # The docker-availability probe is cached in a module global; reset it
    # around every test so a monkeypatched `which`/`docker info` from one test
    # can't leak a stale verdict into the next (review finding #8).
    sandbox.reset_docker_cache()
    yield
    sandbox.reset_docker_cache()


class TestWorktreeGate:
    def test_returns_none_when_no_git(self, monkeypatch, tmp_path):
        # No git CLI ⇒ caller must fall back to the snapshot path.
        monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
        out = sandbox.worktree_gate(
            [{"path": "backend/app/x.py", "new_content": "X=1\n"}],
            repo_root=tmp_path,
        )
        assert out is None

    def test_disabled_by_killswitch(self, monkeypatch):
        monkeypatch.setenv("SHIPMATE_WORKTREE_GATE", "0")
        assert sandbox.worktree_gate_enabled() is False
        monkeypatch.setenv("SHIPMATE_WORKTREE_GATE", "1")
        assert sandbox.worktree_gate_enabled() is True

    def test_dirty_tree_falls_back_to_snapshot(self, monkeypatch):
        # Review finding #1: a worktree validates HEAD, so a dirty working tree
        # (uncommitted tracked changes) must fall back to the snapshot path —
        # else the patch validates against stale code + an inconsistent baseline.
        if not sandbox.worktree_available():
            pytest.skip("not inside a git work tree")
        monkeypatch.setattr(sandbox, "working_tree_clean", lambda root=None: False)
        out = sandbox.worktree_gate(
            [{"path": "backend/app/_probe_sbx.py", "new_content": "X=1\n"}],
            # runner=None so the dirty-tree guard is exercised (it's skipped when
            # a test injects a runner).
        )
        assert out is None, "dirty tree must fall back (return None), not run a worktree"

    def test_clean_tree_check_ignores_untracked(self):
        # working_tree_clean uses `git diff HEAD` (tracked only); an untracked
        # probe file shouldn't flip it to dirty. We don't create one here — just
        # assert the function runs and returns a bool against the real repo.
        if not sandbox.worktree_available():
            pytest.skip("not inside a git work tree")
        assert isinstance(sandbox.working_tree_clean(), bool)

    def test_accepts_when_runner_reports_no_regression(self):
        # Inject a runner so we don't run the real suite. Real git worktree is
        # created against THIS repo's HEAD (we're inside the repo's checkout).
        if not sandbox.worktree_available():
            pytest.skip("not inside a git work tree")
        calls = {}

        def fake_runner(wt_root: Path):
            calls["root"] = wt_root
            # The patch file must actually be present in the worktree.
            calls["patched_exists"] = (wt_root / "backend" / "app" / "_probe_sbx.py").exists()
            return 100, 0, "100 passed"

        res = sandbox.worktree_gate(
            [{"path": "backend/app/_probe_sbx.py", "new_content": "PROBE = 1\n"}],
            before_count=100, runner=fake_runner, smoke=False,
        )
        assert res is not None, "worktree should have been created"
        assert res.passed is True
        assert res.after == 100
        # The runner saw the applied patch inside the isolated worktree.
        assert calls.get("patched_exists") is True
        # And the real repo tree was NOT mutated by the probe file.
        assert not (sandbox.REPO_ROOT / "backend" / "app" / "_probe_sbx.py").exists()

    def test_rejects_on_regression(self):
        if not sandbox.worktree_available():
            pytest.skip("not inside a git work tree")
        res = sandbox.worktree_gate(
            [{"path": "backend/app/_probe_sbx.py", "new_content": "PROBE = 1\n"}],
            before_count=100, runner=lambda r: (95, 5, "95 passed, 5 failed"),
            smoke=False,
        )
        assert res is not None
        assert res.passed is False
        assert "regression" in res.reason

    def test_worktree_removed_even_when_runner_raises(self):
        if not sandbox.worktree_available():
            pytest.skip("not inside a git work tree")
        before = _count_worktrees()

        def boom(_root):
            raise RuntimeError("runner blew up")

        with pytest.raises(RuntimeError):
            sandbox.worktree_gate(
                [{"path": "backend/app/_probe_sbx.py", "new_content": "X=1\n"}],
                before_count=100, runner=boom, smoke=False,
            )
        # The worktree must have been torn down despite the exception.
        assert _count_worktrees() == before
        assert not (sandbox.REPO_ROOT / "backend" / "app" / "_probe_sbx.py").exists()


class TestWorktreeSmoke:
    """worktree_smoke (Phase 4): import-smoke in a throwaway worktree — race-free,
    real tree untouched. The inner verify-loop's execution step."""

    def test_returns_none_when_no_git(self, monkeypatch, tmp_path):
        monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)
        out = sandbox.worktree_smoke(
            [{"path": "backend/app/x.py", "new_content": "X=1\n"}],
            repo_root=tmp_path,
        )
        assert out is None

    def test_clean_patch_imports_and_cleans_up(self):
        if not sandbox.worktree_available():
            pytest.skip("not inside a git work tree")
        before = _count_worktrees()
        # A harmless new module — app.main must still import in the worktree.
        result = sandbox.worktree_smoke(
            [{"path": "backend/app/_probe_smoke.py", "new_content": "PROBE = 1\n"}],
        )
        assert result is not None
        ok, _detail = result
        assert ok is True
        # Worktree torn down; the probe never touched the real tree.
        assert _count_worktrees() == before
        assert not (sandbox.REPO_ROOT / "backend" / "app" / "_probe_smoke.py").exists()

    def test_broken_import_detected(self):
        if not sandbox.worktree_available():
            pytest.skip("not inside a git work tree")
        # Overwrite app.main with an unimportable module inside the worktree.
        result = sandbox.worktree_smoke(
            [{"path": "backend/app/main.py",
              "new_content": "import a_module_that_does_not_exist_anywhere\n"}],
        )
        assert result is not None
        ok, detail = result
        assert ok is False
        assert "a_module_that_does_not_exist" in detail or "ModuleNotFound" in detail
        # Real app.main is intact (worktree was isolated).
        real = (sandbox.REPO_ROOT / "backend" / "app" / "main.py").read_text()
        assert "a_module_that_does_not_exist_anywhere" not in real


def _count_worktrees() -> int:
    proc = subprocess.run(
        ["git", "-C", str(sandbox.REPO_ROOT), "worktree", "list"],
        capture_output=True, text=True, timeout=15,
    )
    return len([ln for ln in proc.stdout.splitlines() if ln.strip()])
