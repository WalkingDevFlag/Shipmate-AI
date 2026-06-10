"""Tests for target-repo sandbox validation (Refactor #1).

The validation gate used to only run against ShipMate's OWN repo (_SELF_REPO),
so "Build It" on any other repo opened PRs with zero local validation. This
adds a clone+run-their-tests gate — OFF by default (runs untrusted code).

These tests exercise the pure pieces (test-command detection, env-strip,
disabled-by-default, the file-apply guard) and a mocked clone-failure path,
without cloning anything over the network.
"""
import os

import pytest

from app.services import validation_gate as vg
from app.services import sandbox


# ── detect_test_command ──────────────────────────────────────────────────────

def test_detects_pytest_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    cmd = vg.detect_test_command(str(tmp_path))
    assert cmd is not None
    assert "pytest" in cmd


def test_detects_pytest_from_tests_dir(tmp_path):
    (tmp_path / "tests").mkdir()
    cmd = vg.detect_test_command(str(tmp_path))
    assert cmd is not None and "pytest" in cmd


def test_detects_npm_test(tmp_path):
    (tmp_path / "package.json").write_text('{"scripts": {"test": "vitest run"}}')
    cmd = vg.detect_test_command(str(tmp_path))
    assert cmd == ["npm", "test", "--silent"]


def test_skips_cra_placeholder_test_script(tmp_path):
    (tmp_path / "package.json").write_text(
        '{"scripts": {"test": "echo \\"Error: no test specified\\" && exit 1"}}')
    assert vg.detect_test_command(str(tmp_path)) is None


def test_detects_make_test(tmp_path):
    (tmp_path / "Makefile").write_text("test:\n\tpytest\n")
    assert vg.detect_test_command(str(tmp_path)) == ["make", "test"]


def test_no_test_command_when_nothing_present(tmp_path):
    (tmp_path / "README.md").write_text("# hi")
    assert vg.detect_test_command(str(tmp_path)) is None


# ── env strip ────────────────────────────────────────────────────────────────

def test_stripped_env_omits_secrets(monkeypatch):
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "supersecret")
    monkeypatch.setenv("BEDROCK_API_KEY", "bedrocksecret")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "awssecret")
    env = vg._stripped_env()
    assert "GITHUB_CLIENT_SECRET" not in env
    assert "BEDROCK_API_KEY" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    # PATH must survive so the test runner can be found.
    assert "PATH" in env
    assert env.get("SHIPMATE_SANDBOX") == "1"


def test_stripped_env_preserves_loader_paths(monkeypatch):
    """Regression: the dynamic-loader search paths must survive the strip.

    GitHub's setup-python toolcache interpreter is dynamically linked against
    libpython in …/x64/lib and CANNOT START without LD_LIBRARY_PATH. Stripping
    it made the target-repo gate's nested `python -m pytest` exit 127 (loader
    failure) on the Linux CI runner — before pytest ran — while passing on
    macOS. These are our own interpreter's lib paths, not secrets, so they must
    be inherited."""
    monkeypatch.setenv("LD_LIBRARY_PATH", "/opt/hostedtoolcache/Python/3.11/x64/lib")
    monkeypatch.setenv("DYLD_LIBRARY_PATH", "/usr/local/lib")
    monkeypatch.setenv("GITHUB_CLIENT_SECRET", "supersecret")  # still a secret
    env = vg._stripped_env()
    assert env.get("LD_LIBRARY_PATH") == "/opt/hostedtoolcache/Python/3.11/x64/lib"
    assert env.get("DYLD_LIBRARY_PATH") == "/usr/local/lib"
    # …but the strip still drops real secrets.
    assert "GITHUB_CLIENT_SECRET" not in env


# ── flag gating ──────────────────────────────────────────────────────────────

def test_gate_disabled_by_default(monkeypatch):
    monkeypatch.delenv("SHIPMATE_TARGET_REPO_GATE", raising=False)
    assert vg.target_repo_gate_enabled() is False
    # When disabled, gate_patch_target_repo short-circuits to a pass without
    # cloning anything.
    res = vg.gate_patch_target_repo("o", "r", "main", [{"path": "x.py", "new_content": "1"}])
    assert res.passed is True
    assert "disabled" in res.reason


def test_gate_enabled_flag(monkeypatch):
    monkeypatch.setenv("SHIPMATE_TARGET_REPO_GATE", "1")
    assert vg.target_repo_gate_enabled() is True


# ── clone failure path (no network) ──────────────────────────────────────────

def test_clone_failure_returns_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv("SHIPMATE_TARGET_REPO_GATE", "1")

    import subprocess

    class _FailClone:
        returncode = 1
        stdout = ""
        stderr = "fatal: repository not found"

    def fake_run(cmd, *a, **k):
        # Only the clone runs in this path.
        assert cmd[0] == "git" and cmd[1] == "clone"
        return _FailClone()

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = vg.gate_patch_target_repo(
        "o", "r", "main", [{"path": "x.py", "new_content": "1"}], access_token="ghp_tok",
    )
    assert res.passed is False
    assert "clone failed" in res.reason
    # The tokened URL must never leak into the summary.
    assert "ghp_tok" not in res.summary_tail


def test_token_not_leaked_in_clone_url_logging():
    url = vg._safe_clone_url("o", "r", "ghp_SEKRET")
    assert "ghp_SEKRET" in url  # it IS in the URL passed to git…
    # …but the function that builds the error tail scrubs it (covered above).


# ── file-apply traversal guard ───────────────────────────────────────────────

def test_apply_files_rejects_traversal(tmp_path):
    written = vg._apply_files_to_dir(str(tmp_path), [
        {"path": "ok/a.py", "new_content": "x"},
        {"path": "../escape.py", "new_content": "evil"},
        {"path": "/abs.py", "new_content": "evil"},
    ])
    assert written == ["ok/a.py"]
    assert (tmp_path / "ok" / "a.py").exists()
    assert not (tmp_path.parent / "escape.py").exists()


# ── end-to-end with a real local "clone" (pytest in a tmp repo) ──────────────

def test_runs_detected_pytest_in_clone(monkeypatch, tmp_path):
    """Patch the clone step to populate the workdir with a tiny passing pytest
    suite, then let the real detect+run path execute. Proves the gate runs the
    TARGET repo's tests and parses the result.

    docker is absent on CI/dev, so the gate now FAILS CLOSED unless the operator
    opts into the unsandboxed host path — set that flag here (this test asserts
    the run+parse mechanics, not the sandbox policy)."""
    monkeypatch.setenv("SHIPMATE_TARGET_REPO_GATE", "1")
    monkeypatch.setenv("SHIPMATE_ALLOW_UNSANDBOXED_TARGET_TESTS", "1")
    # This test exercises the run+parse MECHANICS on the host path (the backend
    # venv has pytest). Pin docker OFF so it deterministically takes that path —
    # on a docker-equipped runner (GitHub) the gate would otherwise launch the
    # suite inside python:3.x-slim, which has no pytest installed.
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)

    import subprocess
    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        # cmd is argv; the clone is `git clone ... <workdir>` where workdir ends in /repo.
        if cmd[:2] == ["git", "clone"]:
            workdir = cmd[-1]
            os.makedirs(os.path.join(workdir, "tests"), exist_ok=True)
            with open(os.path.join(workdir, "tests", "test_smoke.py"), "w") as f:
                f.write("def test_ok():\n    assert 1 + 1 == 2\n")

            class _OK:
                returncode = 0
                stdout = ""
                stderr = ""
            return _OK()
        if cmd[:3] == ["git", "remote", "set-url"] or cmd[:2] == ["git", "-C"] and "set-url" in cmd:
            class _OK:
                returncode = 0
                stdout = ""
                stderr = ""
            return _OK()
        # The actual pytest invocation runs for real against the clone.
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = vg.gate_patch_target_repo(
        "o", "r", "main", [{"path": "app/x.py", "new_content": "X = 1\n"}],
    )
    assert res.passed is True, res.reason
    assert res.failed == 0
    assert res.before == 1, "the one passing test must be counted (detection fired, pytest ran)"


def test_failing_target_tests_reject_the_patch(monkeypatch):
    monkeypatch.setenv("SHIPMATE_TARGET_REPO_GATE", "1")
    monkeypatch.setenv("SHIPMATE_ALLOW_UNSANDBOXED_TARGET_TESTS", "1")
    # Host path (backend venv has pytest); pin docker OFF — see the note in
    # test_runs_detected_pytest_in_clone.
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)

    import subprocess
    real_run = subprocess.run

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "clone"]:
            workdir = cmd[-1]
            os.makedirs(os.path.join(workdir, "tests"), exist_ok=True)
            with open(os.path.join(workdir, "tests", "test_broken.py"), "w") as f:
                f.write("def test_fail():\n    assert 1 == 2\n")

            class _OK:
                returncode = 0
                stdout = ""
                stderr = ""
            return _OK()
        if "set-url" in cmd:
            class _OK:
                returncode = 0
                stdout = ""
                stderr = ""
            return _OK()
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = vg.gate_patch_target_repo(
        "o", "r", "main", [{"path": "app/x.py", "new_content": "X = 1\n"}],
    )
    assert res.passed is False
    assert res.failed >= 1
    assert "failed" in res.reason


def test_clone_credential_scrubbed_before_running_suite(monkeypatch):
    """Review finding #4: the tokened clone URL git persists into .git/config
    must be scrubbed (remote set-url to the tokenless URL) BEFORE the untrusted
    suite runs, so the suite can't lift the bearer token from disk."""
    monkeypatch.setenv("SHIPMATE_TARGET_REPO_GATE", "1")
    monkeypatch.setenv("SHIPMATE_ALLOW_UNSANDBOXED_TARGET_TESTS", "1")
    # Host path (backend venv has pytest); pin docker OFF — see the note in
    # test_runs_detected_pytest_in_clone.
    monkeypatch.setattr(sandbox, "docker_available", lambda: False)

    import subprocess
    real_run = subprocess.run
    seen = {"scrub_before_tests": False, "tests_ran": False}

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "clone"]:
            workdir = cmd[-1]
            os.makedirs(os.path.join(workdir, "tests"), exist_ok=True)
            with open(os.path.join(workdir, "tests", "test_ok.py"), "w") as f:
                f.write("def test_ok():\n    assert True\n")

            class _OK:
                returncode = 0; stdout = ""; stderr = ""
            return _OK()
        if "set-url" in cmd:
            # The scrub rewrites origin to the tokenless URL.
            assert "https://github.com/o/r.git" in cmd
            assert not any("x-access-token" in str(c) for c in cmd)
            if not seen["tests_ran"]:
                seen["scrub_before_tests"] = True

            class _OK:
                returncode = 0; stdout = ""; stderr = ""
            return _OK()
        if "pytest" in cmd:
            seen["tests_ran"] = True
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = vg.gate_patch_target_repo(
        "o", "r", "main", [{"path": "app/x.py", "new_content": "X=1\n"}],
        access_token="ghp_SEKRET",
    )
    assert seen["scrub_before_tests"] is True, "credential must be scrubbed before tests run"
    assert "ghp_SEKRET" not in (res.summary_tail or "")


def test_fails_closed_without_docker_or_opt_in(monkeypatch):
    """The security fix: with docker absent and no explicit opt-in, the gate must
    NOT execute the untrusted suite on the host. It returns a non-blocking pass
    (PR ships unvalidated, pre-Phase-2 behaviour) WITHOUT running clone code."""
    monkeypatch.setenv("SHIPMATE_TARGET_REPO_GATE", "1")
    monkeypatch.delenv("SHIPMATE_ALLOW_UNSANDBOXED_TARGET_TESTS", raising=False)

    import subprocess
    from app.services import sandbox
    sandbox.reset_docker_cache()
    monkeypatch.setattr(sandbox.shutil, "which", lambda _: None)  # no docker

    real_run = subprocess.run
    ran_tests = {"pytest": False}

    def fake_run(cmd, *a, **k):
        if cmd[:2] == ["git", "clone"]:
            workdir = cmd[-1]
            os.makedirs(os.path.join(workdir, "tests"), exist_ok=True)
            with open(os.path.join(workdir, "tests", "test_x.py"), "w") as f:
                f.write("def test_ok():\n    assert True\n")

            class _OK:
                returncode = 0; stdout = ""; stderr = ""
            return _OK()
        if "pytest" in cmd:
            ran_tests["pytest"] = True
        if "set-url" in cmd:
            class _OK:
                returncode = 0; stdout = ""; stderr = ""
            return _OK()
        return real_run(cmd, *a, **k)

    monkeypatch.setattr(subprocess, "run", fake_run)
    res = vg.gate_patch_target_repo(
        "o", "r", "main", [{"path": "app/x.py", "new_content": "X=1\n"}],
    )
    sandbox.reset_docker_cache()
    assert ran_tests["pytest"] is False, "untrusted suite must NOT run on host without isolation"
    assert "docker unavailable" in res.reason
    assert res.passed is True  # fail-open verdict, but no code executed
