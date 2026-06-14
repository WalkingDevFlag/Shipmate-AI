"""
eval_runner — execute a ValidationSpec and produce an EvalReport (Phase 5).

This is the EvalOps counterpart of ValidationGate.run_pytest: it BOOTS the app,
fires each scenario's HTTP request, evaluates the assertions (scenario_runner),
checks the metric sink and the captured logs, and assembles an EvalReport. The
report is the pluggable SIGNAL that generalizes test_synthesizer's coverage
delta — "did this artifact make the scenarios pass / the metric fire / the logs
stay clean?".

Two execution modes, mirroring the validation gate:

  • IN-PROCESS (run_eval) — boots the real FastAPI app with Starlette's
    TestClient in THIS process, against an isolated metrics sink + a log capture
    handler. Cheap, no subprocess, used for ShipMate's own app (the dogfood
    case) and for unit tests (the app is injected). Scenarios hit real route
    handlers, so a 200 with a clean log is a genuine end-to-end signal.

  • SANDBOXED (run_eval_in_worktree) — for validating a PATCH: applies the
    files in a throwaway git worktree (Phase 2, race-free) and runs the eval in
    a child process there, so the spec is checked against the PATCHED code
    without mutating the real tree. Falls back to in-process when no worktree.

Always writes eval-report.json (when out_path given) for CI / the CIWatcher to
consume, the same way it already consumes JUnit XML. Fail-open: a boot/timeout
failure becomes EvalReport(error=...), never an exception into the caller.
"""
from __future__ import annotations

import json
import logging
import os
import threading
from typing import Any, Callable, List, Optional

from app.services import metrics as metrics_sink
from app.services import scenario_runner as sr
from app.services.eval_schemas import EvalReport, ScenarioResult, ValidationSpec

logger = logging.getLogger("shipmate.eval_runner")

# An app factory: () -> ASGI app. Injected so unit tests pass a tiny app and
# the orchestrator passes the real one (app.main:app). Default lazy-imports the
# real ShipMate app.
AppFactory = Callable[[], Any]

# run_eval mutates PROCESS-GLOBAL state (os.environ['SHIPMATE_METRICS_FILE'],
# spec.environment keys, a root-logger handler) for the duration of a run. The
# codebase runs analyze on background threads, so two run_eval calls could
# overlap and clobber each other's metrics sink / env / captured logs (review
# findings F4/F11). Serialize the global-state window with this lock — eval is
# not on the hot per-request path, so serializing is cheap insurance.
_EVAL_LOCK = threading.Lock()

# Safe HTTP methods a scenario may use WITHOUT opt-in. run_eval boots the REAL
# app and fires these at REAL handlers; a model-emitted spec must not POST/PUT/
# DELETE against a mutating endpoint by default (review finding F1). A scenario
# with a non-safe method is reported as a failure unless allow_mutating=True.
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _default_app_factory() -> Any:
    from app.main import app
    return app


class _LogCapture(logging.Handler):
    """Captures emitted log records during a scenario run so the log-clean
    expectation can be evaluated. Attached to the root logger for the duration
    of run_eval, then removed.

    Captures only OUR loggers by default (review finding F12): attached to the
    root logger it would see every third-party library's records, so an
    unrelated library logging at ERROR would false-fail the log-clean check. We
    keep only records whose logger name is in the app's namespace (`shipmate.*`,
    `app.*`, `uvicorn*`) — the surface a patch can actually break. Tests inject
    their own logger names via `extra_prefixes`."""
    _DEFAULT_PREFIXES = ("shipmate", "app", "uvicorn")

    def __init__(self, extra_prefixes: tuple = ()) -> None:
        super().__init__(level=logging.DEBUG)
        self.lines: List[str] = []
        self._prefixes = self._DEFAULT_PREFIXES + tuple(extra_prefixes)

    def emit(self, record: logging.LogRecord) -> None:  # noqa: D401
        try:
            name = record.name or ""
            if not any(name == p or name.startswith(p + ".") for p in self._prefixes):
                return
            self.lines.append(f"{record.levelname} {record.name}: {record.getMessage()}")
        except Exception:  # pragma: no cover - never let logging break the run
            pass

    @property
    def text(self) -> str:
        return "\n".join(self.lines)


def run_eval(
    spec: ValidationSpec,
    app_factory: Optional[AppFactory] = None,
    *,
    out_path: Optional[str] = None,
    allow_mutating: bool = False,
) -> EvalReport:
    """Boot the app in-process (TestClient), run every scenario, evaluate
    metrics + logs, and return (and optionally write) the EvalReport.

    Metrics are routed to an ISOLATED sink file for the duration of the run, so
    `fired_names` reflects exactly this eval's emissions. The app's logs are
    captured via a root-logger handler. Always restores the metrics-sink env and
    detaches the handler in `finally`.

    NOTE: this boots the REAL app and fires each scenario's request at REAL
    handlers. By default only SAFE methods (GET/HEAD/OPTIONS) run — a scenario
    with a mutating method (POST/PUT/PATCH/DELETE) is reported as a failure so a
    model-emitted spec can't mutate live state. Pass allow_mutating=True only
    when the app is a throwaway (the worktree child) or you trust the spec.

    The process-global window (env + metrics sink + root-logger handler) is
    serialized by _EVAL_LOCK so concurrent evals don't clobber each other."""
    app_factory = app_factory or _default_app_factory

    if spec.is_empty():
        report = EvalReport(spec_name=spec.name, passed=True)
        _maybe_write(report, out_path)
        return report

    with _EVAL_LOCK:
        return _run_eval_locked(spec, app_factory, out_path, allow_mutating)


def _run_eval_locked(spec, app_factory, out_path, allow_mutating) -> EvalReport:
    # Export any spec-declared environment (feature flags etc.) for the run.
    saved_env = {k: os.environ.get(k) for k in spec.environment}
    # Isolated metrics sink so we read back ONLY this run's metrics.
    import tempfile
    sink_fd, sink_file = tempfile.mkstemp(prefix="shipmate_eval_metrics_", suffix=".jsonl")
    os.close(sink_fd)
    saved_sink = os.environ.get("SHIPMATE_METRICS_FILE")

    capture = _LogCapture()
    root = logging.getLogger()
    root.addHandler(capture)
    try:
        for k, v in spec.environment.items():
            os.environ[k] = v
        os.environ["SHIPMATE_METRICS_FILE"] = sink_file
        metrics_sink.reset(sink_file)

        try:
            from starlette.testclient import TestClient
            app = app_factory()
            # Bare construction (NOT `with TestClient(app) as client`) is
            # DELIBERATE (review F5/F6): Starlette runs the ASGI lifespan only
            # in __enter__, so the app's startup hook (sqlite init_all +
            # CIWatcher.resume_from_db + store writes) does NOT fire here. That
            # keeps an eval side-effect-free — it exercises route handlers, not
            # the full boot. It's a route-level signal, not a lifespan one; a
            # spec that needs startup state should assert it explicitly.
            client = TestClient(app)
        except Exception as e:
            report = EvalReport(spec_name=spec.name, passed=False,
                                error=f"app boot failed: {e}")
            _maybe_write(report, out_path)
            return report

        scenario_results: List[ScenarioResult] = []
        for scn in spec.scenarios:
            method = scn.method.upper()
            if method not in _SAFE_METHODS and not allow_mutating:
                scenario_results.append(ScenarioResult(
                    name=scn.name, passed=False, status_code=None,
                    failures=[
                        f"scenario uses mutating method {method} but allow_mutating "
                        f"is False — refused to fire it at the live app"
                    ],
                ))
                continue
            try:
                resp = client.request(
                    method, scn.url,
                    json=scn.json_body, headers=scn.headers or None,
                )
                try:
                    body = resp.json()
                except Exception:
                    body = {"_raw": resp.text}
                scenario_results.append(sr.eval_scenario(scn, resp.status_code, body))
            except Exception as e:
                scenario_results.append(ScenarioResult(
                    name=scn.name, passed=False, status_code=None,
                    failures=[f"request raised: {e}"],
                ))

        fired = metrics_sink.fired_names(sink_file)
        metric_results = sr.eval_metrics(spec.metrics, fired)
        log_clean, log_violations = sr.eval_logs(capture.text, spec.logs)

        report = sr.assemble_report(
            spec, scenario_results, metric_results, log_clean, log_violations,
        )
        _maybe_write(report, out_path)
        logger.info("eval %s: %s (%d scenario(s))",
                    spec.name, "PASS" if report.passed else "FAIL",
                    len(scenario_results))
        return report
    finally:
        root.removeHandler(capture)
        # Restore env.
        for k, v in saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        if saved_sink is None:
            os.environ.pop("SHIPMATE_METRICS_FILE", None)
        else:
            os.environ["SHIPMATE_METRICS_FILE"] = saved_sink
        try:
            os.remove(sink_file)
        except OSError:
            pass


def _maybe_write(report: EvalReport, out_path: Optional[str]) -> None:
    """Write eval-report.json for CI / CIWatcher consumption. Fail-open."""
    if not out_path:
        return
    try:
        os.makedirs(os.path.dirname(out_path) or ".", exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as f:
            json.dump(report.model_dump(), f, indent=2)
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("eval report write failed (%s)", e)


def run_eval_in_worktree(
    spec: ValidationSpec,
    files: List[dict],
    *,
    ref: str = "HEAD",
    out_path: Optional[str] = None,
    trusted: bool = False,
) -> Optional[EvalReport]:
    """Validate a PATCH: apply `files` in a throwaway worktree and run the spec
    against the patched code in a child process, so the eval reflects the patch
    without mutating the real tree. Returns None when no worktree can be created
    (caller falls back to in-process run_eval on the current tree, or skips).

    The child invokes this module's `_worktree_entry` via `python -m`, which
    boots the worktree's app and prints the EvalReport JSON to stdout.

    SECURITY (review finding F2): this BOOTS a patched `app.main` in a child
    process — i.e. it RUNS code that may be untrusted (a patch for an arbitrary
    target repo). By default (`trusted=False`) the child gets a STRIPPED env via
    sandbox.stripped_env with an ephemeral HOME, so it can't read the operator's
    GitHub token / AWS / Bedrock keys / ~/.aws / ~/.ssh even though it runs on
    the host — the same floor the Phase-2 target-repo gate uses. Pass
    trusted=True ONLY for ShipMate's own dogfood patch, where the child needs the
    real env to boot the full app. (A real jail for untrusted code is the
    Phase-2 docker path; this is the env-strip floor, not a VM.)"""
    from app.services import sandbox
    if not sandbox.worktree_available():
        return None

    import subprocess
    import tempfile
    from pathlib import Path

    with sandbox._worktree(ref, sandbox.REPO_ROOT) as wt_path:
        if wt_path is None:
            return None
        sandbox._apply_files_to_dir(str(wt_path), files)
        backend = Path(wt_path) / "backend"
        spec_json = spec.model_dump_json()
        store_dir = tempfile.mkdtemp(prefix="shipmate_eval_store_")
        if trusted:
            # Dogfood: the real app needs the real env to boot. Still isolate
            # the store so the child can't clobber live sqlite state.
            env = dict(os.environ)
        else:
            # Untrusted patch: minimal env, ephemeral HOME — no secrets reachable.
            home_dir = tempfile.mkdtemp(prefix="shipmate_eval_home_")
            env = sandbox.stripped_env(home_dir)
            env["PYTHONPATH"] = str(backend)
        env["SHIPMATE_STORE_DIR"] = store_dir
        env["SHIPMATE_WORKTREE_GATE"] = "0"
        try:
            proc = subprocess.run(
                [sandbox.PYTHON, "-m", "app.services.eval_runner", "--spec-stdin"],
                input=spec_json, cwd=str(backend), capture_output=True, text=True,
                timeout=int(os.getenv("SHIPMATE_EVAL_TIMEOUT_S", "120")), env=env,
            )
        except subprocess.TimeoutExpired:
            return EvalReport(spec_name=spec.name, passed=False, error="eval timeout")
        finally:
            import shutil
            shutil.rmtree(store_dir, ignore_errors=True)

        report = _parse_child_report(proc.stdout, spec)
        _maybe_write(report, out_path)
        return report


def _parse_child_report(stdout: str, spec: ValidationSpec) -> EvalReport:
    """Parse the EvalReport JSON the child printed on a line prefixed with the
    sentinel. Falls back to an error report if the child emitted nothing valid."""
    for line in (stdout or "").splitlines():
        if line.startswith("__EVAL_REPORT__ "):
            try:
                return EvalReport.model_validate_json(line[len("__EVAL_REPORT__ "):])
            except Exception:
                break
    return EvalReport(spec_name=spec.name, passed=False,
                      error="child produced no parseable eval report")


def _worktree_entry() -> None:
    """`python -m app.services.eval_runner --spec-stdin`: read a ValidationSpec
    JSON from stdin, run it in-process (we ARE the worktree's process now), and
    print the report on a sentinel line for the parent to parse."""
    import sys
    spec_json = sys.stdin.read()
    spec = ValidationSpec.model_validate_json(spec_json)
    report = run_eval(spec)
    print("__EVAL_REPORT__ " + report.model_dump_json())


def _file_entry(spec_path: str, out_path: Optional[str]) -> int:
    """`python -m app.services.eval_runner --spec-file <path> [--out <path>]`:
    run a committed ValidationSpec against the live app (CI eval-gate). Writes
    eval-report.json and exits non-zero on failure so the CI job goes red."""
    with open(spec_path, "r", encoding="utf-8") as f:
        spec = ValidationSpec.model_validate_json(f.read())
    report = run_eval(spec, out_path=out_path)
    print(json.dumps(report.model_dump(), indent=2))
    return 0 if report.passed else 1


if __name__ == "__main__":  # pragma: no cover - exercised via subprocess / CI
    import sys
    if "--spec-stdin" in sys.argv:
        _worktree_entry()
    elif "--spec-file" in sys.argv:
        i = sys.argv.index("--spec-file")
        spec_path = sys.argv[i + 1]
        out = None
        if "--out" in sys.argv:
            out = sys.argv[sys.argv.index("--out") + 1]
        sys.exit(_file_entry(spec_path, out))
