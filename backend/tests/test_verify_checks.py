"""Concrete cheap checks for the inner verify-loop (Phase 4).

These adapt ShipMate's existing validators (ast_lint via _lint_coder_output,
scope_guard, sandbox import-smoke) into the verify_loop.Check shape. The smoke
check is sized by the finding's gate tier and runs in a throwaway worktree.

Tests pin: lint catches hallucinated imports and flags a syntax error FATAL;
scope catches a mass-drop rewrite; the smoke check no-ops for a docs/lint-tier
finding; default_checks composes the right set and honours include_smoke. They
use real CoderOutput objects but avoid running the full suite.
"""
import pytest

from app.agents.coder_agent import CoderOutput, CoderFile
from app.services import verify_checks


def _out(path, content, summary="a sufficiently detailed summary of the change"):
    return CoderOutput(
        files=[CoderFile(path=path, new_content=content, rationale="r")],
        summary=summary,
    )


# ── lint check ───────────────────────────────────────────────────────────────

class TestLintCheck:
    def test_clean_patch_passes(self):
        check = verify_checks.make_lint_check({"h.py": "x = 1\n"}, ["h.py"])
        res = check(_out("h.py", "x = 2\n"))
        assert res.ok
        assert res.fatal is False

    def test_hallucinated_import_flagged(self):
        check = verify_checks.make_lint_check(
            {"backend/app/x.py": "y = 1\n"}, ["backend/app/x.py"],
        )
        res = check(_out("backend/app/x.py",
                         "from app.services.nope_does_not_exist import thing\ny = 1\n"))
        assert not res.ok
        assert any("hallucinated" in i.lower() or "don't exist" in i.lower()
                   or "not in repo" in i.lower() for i in res.issues)

    def test_syntax_error_is_fatal(self):
        check = verify_checks.make_lint_check({"h.py": "x = 1\n"}, ["h.py"])
        res = check(_out("h.py", "def broken(:\n  pass\n"))
        assert not res.ok
        assert res.fatal is True

    def test_checker_error_is_fail_open(self, monkeypatch):
        # If _lint_coder_output blows up, the check passes (never blocks).
        import app.services.coder_orchestrator as co
        monkeypatch.setattr(co, "_lint_coder_output",
                            lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
        check = verify_checks.make_lint_check({}, [])
        res = check(_out("h.py", "x=1\n"))
        assert res.ok


# ── scope check ──────────────────────────────────────────────────────────────

class TestScopeCheck:
    def test_clean_edit_passes(self):
        original = "def a(): pass\ndef b(): pass\n"
        check = verify_checks.make_scope_check({"m.py": original})
        res = check(_out("m.py", "def a(): pass\ndef b(): return 1\n"))
        assert res.ok

    def test_mass_drop_flagged(self):
        original = "\n".join(f"def fn{i}(): pass" for i in range(10))
        check = verify_checks.make_scope_check({"big.py": original})
        res = check(_out("big.py", "def fn0(): pass\n", summary="refactor"))
        assert not res.ok
        assert any("DROPS" in i for i in res.issues)


# ── smoke check (tier-sized) ─────────────────────────────────────────────────

class TestSmokeCheck:
    def test_docs_tier_is_noop_pass(self):
        # A docs finding → lint tier (run_smoke False) → smoke is a no-op pass,
        # without touching the tree or a worktree.
        check = verify_checks.make_smoke_check("milestone", "docs")
        res = check(_out("README.md", "# hi\n"))
        assert res.ok

    def test_empty_files_passes(self):
        check = verify_checks.make_smoke_check("guardrail", "injection")
        res = check(CoderOutput(files=[], summary="nothing"))
        assert res.ok

    def test_broken_import_flagged_via_worktree(self, monkeypatch):
        # Force the worktree smoke to report a broken import; the check must
        # surface an actionable issue.
        from app.services import sandbox
        monkeypatch.setattr(sandbox, "worktree_smoke",
                            lambda files, **k: (False, "ModuleNotFoundError: no_such_mod"))
        check = verify_checks.make_smoke_check("guardrail", "injection")
        res = check(_out("backend/app/x.py", "import no_such_mod\n"))
        assert not res.ok
        assert any("import" in i.lower() for i in res.issues)

    def test_worktree_pass_is_clean(self, monkeypatch):
        from app.services import sandbox
        monkeypatch.setattr(sandbox, "worktree_smoke", lambda files, **k: (True, ""))
        check = verify_checks.make_smoke_check("guardrail", "injection")
        res = check(_out("backend/app/x.py", "x = 1\n"))
        assert res.ok

    def test_no_worktree_falls_back_to_snapshot_smoke(self, monkeypatch):
        from app.services import sandbox, validation_gate as vg
        monkeypatch.setattr(sandbox, "worktree_smoke", lambda files, **k: None)
        calls = {"snap": 0}
        monkeypatch.setattr(vg, "snapshot_files", lambda p: (calls.__setitem__("snap", 1) or {}))
        monkeypatch.setattr(vg, "write_files_to_tree", lambda f: [])
        monkeypatch.setattr(vg, "smoke_imports", lambda: (True, ""))
        monkeypatch.setattr(vg, "restore_snapshot", lambda s: None)
        check = verify_checks.make_smoke_check("guardrail", "injection")
        res = check(_out("backend/app/x.py", "x = 1\n"))
        assert res.ok
        assert calls["snap"] == 1  # snapshot fallback was used


# ── default_checks composition ───────────────────────────────────────────────

class TestDefaultChecks:
    def test_includes_smoke_by_default(self):
        checks = verify_checks.default_checks("guardrail", "injection", {}, [])
        assert len(checks) == 3  # lint + scope + smoke

    def test_excludes_smoke_when_disabled(self):
        checks = verify_checks.default_checks(
            "guardrail", "injection", {}, [], include_smoke=False,
        )
        assert len(checks) == 2  # lint + scope only

    def test_composed_checks_are_callable(self):
        checks = verify_checks.default_checks("test", "testing", {"h.py": "x=1\n"}, ["h.py"])
        for c in checks:
            res = c(_out("h.py", "x=2\n"))
            assert hasattr(res, "ok")
