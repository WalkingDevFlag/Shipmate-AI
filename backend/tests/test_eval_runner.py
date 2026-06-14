"""eval_runner + define_validation + eval_schemas (Phase 5 EvalOps).

run_eval is exercised against a TINY injected FastAPI app (hermetic — no real
ShipMate app, no network), proving the boot→fire→assert→metric→log pipeline and
that metrics emitted during a request are read back from the isolated sink.
define_validation's provider/minimal-spec behaviour is pinned with a stub.
"""
import pytest
from fastapi import FastAPI

from app.schemas.api_schemas import FindingPayload
from app.services import define_validation, eval_runner, metrics
from app.services.eval_schemas import (
    Assertion, MetricExpectation, Scenario, ValidationSpec,
)


# ── A tiny app the runner boots in-process ───────────────────────────────────

def _tiny_app() -> FastAPI:
    app = FastAPI()

    @app.get("/health")
    def health():
        return {"status": "healthy", "score": 88}

    @app.get("/emits")
    def emits():
        metrics.emit("widget_served")
        return {"ok": True}

    @app.get("/boom")
    def boom():
        import logging
        # app.* namespace → captured by _LogCapture's default prefixes.
        logging.getLogger("app.tinyapp").error("Traceback: simulated failure")
        return {"ok": True}

    @app.get("/thirdparty-noise")
    def thirdparty_noise():
        import logging
        # A non-app library logging at ERROR must NOT fail the eval (F12).
        logging.getLogger("some_third_party_lib").error("noisy but irrelevant")
        return {"ok": True}

    return app


class TestRunEval:
    def test_passing_spec(self):
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="health", url="/health", expect_status=200,
                     assertions=[Assertion(path="status", equals="healthy"),
                                 Assertion(path="score", gte=80)]),
        ])
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app)
        assert rep.passed is True
        assert rep.scenarios[0].passed is True

    def test_failing_assertion(self):
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="health", url="/health",
                     assertions=[Assertion(path="score", gte=99)]),
        ])
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app)
        assert rep.passed is False
        assert any("below gte" in f for f in rep.scenarios[0].failures)

    def test_metric_expectation_satisfied(self):
        spec = ValidationSpec(name="t",
            scenarios=[Scenario(name="emit", url="/emits", expect_status=200)],
            metrics=[MetricExpectation(name="widget_served", must_fire=True)],
        )
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app)
        assert rep.passed is True
        assert rep.metrics[0].fired is True and rep.metrics[0].satisfied is True

    def test_metric_not_fired_fails(self):
        spec = ValidationSpec(name="t",
            scenarios=[Scenario(name="health", url="/health", expect_status=200)],
            metrics=[MetricExpectation(name="never_emitted", must_fire=True)],
        )
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app)
        assert rep.passed is False
        assert rep.metrics[0].satisfied is False

    def test_dirty_logs_fail(self):
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="boom", url="/boom", expect_status=200),
        ])
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app)
        # Scenario returns 200, but the handler logged a Traceback → log-unclean.
        assert rep.passed is False
        assert rep.log_clean is False
        assert rep.log_violations

    def test_thirdparty_error_log_is_ignored(self):
        # A third-party library logging ERROR must NOT fail the log-clean check.
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="noise", url="/thirdparty-noise", expect_status=200)])
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app)
        assert rep.passed is True
        assert rep.log_clean is True

    def test_mutating_method_refused_by_default(self):
        # A POST scenario must be refused unless allow_mutating=True (F1).
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="mutate", method="POST", url="/health", expect_status=200)])
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app)
        assert rep.passed is False
        assert any("mutating method POST" in f for f in rep.scenarios[0].failures)

    def test_mutating_method_allowed_with_optin(self):
        # /health is GET-only so POST 405s, but the point is it's FIRED (not
        # refused) when allow_mutating=True — the failure is a real 405, not a refusal.
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="mutate", method="POST", url="/health", expect_status=200)])
        rep = eval_runner.run_eval(spec, app_factory=_tiny_app, allow_mutating=True)
        assert rep.scenarios[0].status_code == 405  # fired, got method-not-allowed
        assert not any("refused" in f for f in rep.scenarios[0].failures)

    def test_empty_spec_is_vacuous_pass(self):
        rep = eval_runner.run_eval(ValidationSpec(name="empty"), app_factory=_tiny_app)
        assert rep.passed is True
        assert rep.scenarios == []

    def test_boot_failure_is_error_not_raise(self):
        def bad_factory():
            raise RuntimeError("cannot construct app")
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="health", url="/health")])
        rep = eval_runner.run_eval(spec, app_factory=bad_factory)
        assert rep.passed is False
        assert rep.ran is False
        assert "boot failed" in (rep.error or "")

    def test_writes_report_file(self, tmp_path):
        out = str(tmp_path / "eval-report.json")
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="health", url="/health", expect_status=200)])
        eval_runner.run_eval(spec, app_factory=_tiny_app, out_path=out)
        import json, os
        assert os.path.exists(out)
        data = json.loads(open(out).read())
        assert data["spec_name"] == "t" and data["passed"] is True

    def test_metrics_env_restored_after_run(self, monkeypatch):
        monkeypatch.setenv("SHIPMATE_METRICS_FILE", "/original/path.jsonl")
        spec = ValidationSpec(name="t", scenarios=[
            Scenario(name="health", url="/health", expect_status=200)])
        eval_runner.run_eval(spec, app_factory=_tiny_app)
        import os
        assert os.environ["SHIPMATE_METRICS_FILE"] == "/original/path.jsonl"


class TestWorktreeEnvStripping:
    """run_eval_in_worktree (F2): an untrusted patch's child process must get a
    STRIPPED env (no secrets), a trusted dogfood patch gets the real env."""

    def _capture_env(self, monkeypatch):
        from app.services import sandbox
        captured = {}

        # Pretend a worktree is available and yield a fake path.
        monkeypatch.setattr(sandbox, "worktree_available", lambda *a, **k: True)

        import contextlib
        @contextlib.contextmanager
        def fake_wt(ref, root):
            yield "/tmp/fake-wt"
        monkeypatch.setattr(sandbox, "_worktree", fake_wt)
        monkeypatch.setattr(sandbox, "_apply_files_to_dir", lambda *a, **k: None)

        import subprocess
        def fake_run(cmd, **kw):
            captured["env"] = kw.get("env", {})
            class _P:
                stdout = '__EVAL_REPORT__ {"spec_name":"s","passed":true}'
                stderr = ""
            return _P()
        monkeypatch.setattr(subprocess, "run", fake_run)
        return captured

    def test_untrusted_child_env_has_no_secrets(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws_secret")
        captured = self._capture_env(monkeypatch)
        eval_runner.run_eval_in_worktree(
            ValidationSpec(name="s", scenarios=[
                Scenario(name="h", url="/health", expect_status=200)]),
            [{"path": "backend/app/x.py", "new_content": "x=1\n"}],
            trusted=False,
        )
        env = captured["env"]
        assert "GITHUB_TOKEN" not in env
        assert "AWS_SECRET_ACCESS_KEY" not in env
        assert env.get("SHIPMATE_SANDBOX") == "1"   # stripped_env marker

    def test_trusted_child_keeps_real_env(self, monkeypatch):
        monkeypatch.setenv("GITHUB_TOKEN", "ghp_secret")
        captured = self._capture_env(monkeypatch)
        eval_runner.run_eval_in_worktree(
            ValidationSpec(name="s", scenarios=[
                Scenario(name="h", url="/health", expect_status=200)]),
            [{"path": "backend/app/x.py", "new_content": "x=1\n"}],
            trusted=True,
        )
        assert captured["env"].get("GITHUB_TOKEN") == "ghp_secret"


class TestDefineValidation:
    def test_no_provider_gives_minimal_health_spec(self):
        f = FindingPayload(kind="milestone", id="M1", title="Add a thing", description="d")
        spec = define_validation.define_spec(f, provider=None)
        assert not spec.is_empty()
        assert any(s.url == "/health" for s in spec.scenarios)

    def test_provider_spec_gets_health_baseline_injected(self):
        class _Stub:
            def invoke_structured_sync(self, **k):
                return ValidationSpec(name="feature", scenarios=[
                    Scenario(name="feature-check", url="/api/widgets", expect_status=200)])
        f = FindingPayload(kind="milestone", id="M1", title="Widgets endpoint", description="d")
        spec = define_validation.define_spec(f, provider=_Stub())
        assert any(s.url == "/health" for s in spec.scenarios)   # baseline prepended
        assert any(s.url == "/api/widgets" for s in spec.scenarios)
        assert spec.scenarios[0].url == "/health"                # FIRST

    def test_provider_error_falls_back_to_minimal(self):
        class _Boom:
            def invoke_structured_sync(self, **k):
                raise RuntimeError("provider down")
        f = FindingPayload(kind="milestone", id="M1", title="x", description="d")
        spec = define_validation.define_spec(f, provider=_Boom())
        assert spec.name.startswith("min-")

    def test_empty_model_spec_falls_back_to_minimal(self):
        class _Empty:
            def invoke_structured_sync(self, **k):
                return ValidationSpec(name="nothing")  # no scenarios/metrics
        f = FindingPayload(kind="milestone", id="M1", title="x", description="d")
        spec = define_validation.define_spec(f, provider=_Empty())
        assert spec.name.startswith("min-")
