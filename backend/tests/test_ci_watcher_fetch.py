"""CI-fix reliability fixes (the "can't fix CI" bug).

Root cause the loop used to hit: `_invoke_coder` built
`target_files = {"ci_failure.log": blob}` and NOTHING else, so the Coder had to
hallucinate the entire failing file from scratch. These tests cover the four
fixes:

  A1 — `extract_failing_paths` mines the failing source paths from the blob, and
       `_attempt_fix` fetches their CURRENT contents into `target_files` (Coder
       edits a real file instead of rewriting one it can't see).
  A2 — prior failed attempts are fed back into the brief so retries are informed.
  A3 — `eval-report.json` is parsed like JUnit and prepended to the blob.
  A4 — a transient infra flake re-runs the failed jobs ONCE instead of burning a
       Coder attempt on correct code.

The pure pieces (path extraction, eval parsing, transient detection, brief
rendering) are exercised offline; `_attempt_fix` is driven with mocked GitHub +
Coder following the monkeypatch style of test_ci_watcher_early_fail.py.
"""
import asyncio
import json

import pytest

from app.agents.coder_agent import CoderFile, CoderOutput
from app.schemas.api_schemas import FindingPayload, RepoLensSummary
from app.services.ci_watcher import CIWatcher, WatchEntry
from app.services.github_actions_service import (
    CheckRunStatus, GitHubActionsService, extract_failing_paths,
)


def _entry(**kw) -> WatchEntry:
    base = dict(
        owner="o", repo="r", pr_number=7, branch="b", base_branch="main",
        access_token="tok",
        finding=FindingPayload(kind="guardrail", id="B1", title="fix it", description="d"),
        repo_lens=RepoLensSummary(primary_language="Python", entry_points=["backend/app/main.py"]),
    )
    base.update(kw)
    return WatchEntry(**base)


# ── A1: extract_failing_paths (pure) ─────────────────────────────────────────

class TestExtractFailingPaths:
    def test_pulls_pytest_nodes_tracebacks_and_junit_classnames(self):
        blob = (
            "=== STRUCTURED TEST FAILURES (JUnit) ===\n"
            "  [FAILURE] tests.test_auth::TestLogin::test_redirect — AssertionError\n"
            "  [ERROR] tests.services.test_billing::test_charge — ImportError\n"
            "=== check-run: backend-test (conclusion: failure) ===\n"
            "tests/test_auth.py:41: in test_redirect\n"
            '  File "backend/app/services/billing.py", line 88, in charge\n'
        )
        paths = extract_failing_paths(blob)
        assert "tests/test_auth.py" in paths               # pytest node + traceback
        assert "backend/app/services/billing.py" in paths  # traceback File "..."
        assert "tests/services/test_billing.py" in paths   # JUnit dotted classname

    def test_longest_extension_wins_no_tsx_truncation(self):
        # Regression: leftmost-alternation must not clip App.tsx -> App.ts.
        paths = extract_failing_paths("frontend/src/App.tsx:12 error TS2322\nsrc/Foo.jsx:3\n")
        assert "frontend/src/App.tsx" in paths
        assert "frontend/src/App.ts" not in paths
        assert "src/Foo.jsx" in paths

    def test_strips_line_numbers_and_drops_absolute_paths(self):
        paths = extract_failing_paths(
            'File "tests/x.py", line 9\n/usr/lib/python3.12/site.py:100\n'
        )
        assert "tests/x.py" in paths
        assert not any(p.startswith("/") for p in paths)

    def test_dedup_and_cap(self):
        blob = "\n".join(f"pkg/mod{i}.py:{i}" for i in range(20))
        blob += "\ntests/x.py::test_a\ntests/x.py::test_b\n"
        paths = extract_failing_paths(blob)
        assert len(paths) <= 8
        assert paths.count("tests/x.py") <= 1

    def test_empty_blob(self):
        assert extract_failing_paths("") == []
        assert extract_failing_paths(None) == []


# ── A3: eval-report parsing (pure) ───────────────────────────────────────────

class TestEvalReportParsing:
    def _report(self, **over):
        base = {
            "spec_name": "baseline", "passed": False,
            "scenarios": [
                {"name": "health-up", "passed": True, "status_code": 200, "failures": []},
                {"name": "analyze-smoke", "passed": False, "status_code": 422,
                 "failures": ["readiness_score: expected >=0 got null"]},
            ],
            "metrics": [
                {"name": "repo_lens.fired", "fired": False, "satisfied": False},
                {"name": "ok.metric", "fired": True, "satisfied": True},
            ],
            "log_clean": False, "log_violations": ["ERROR boom at app/x.py"],
            "error": None,
        }
        base.update(over)
        return json.dumps(base).encode()

    def test_formats_failing_scenarios_metrics_logs(self):
        out = GitHubActionsService._format_eval_failures(self._report())
        assert out is not None
        assert "analyze-smoke" in out and "HTTP 422" in out
        assert "repo_lens.fired" in out and "never fired" in out
        assert "LOG VIOLATION" in out
        # passing scenario + satisfied metric excluded
        assert "health-up" not in out
        assert "ok.metric" not in out

    def test_clean_report_returns_none(self):
        clean = {"spec_name": "x", "passed": True, "scenarios": [{"name": "a", "passed": True}],
                 "metrics": [], "log_clean": True, "log_violations": [], "error": None}
        assert GitHubActionsService._format_eval_failures(json.dumps(clean).encode()) is None

    def test_top_level_error_surfaced(self):
        out = GitHubActionsService._format_eval_failures(
            self._report(error="boot failed: ImportError", scenarios=[], metrics=[], log_violations=[])
        )
        assert out and "EVAL DID NOT RUN" in out

    def test_garbage_returns_none(self):
        assert GitHubActionsService._format_eval_failures(b"not json") is None
        assert GitHubActionsService._format_eval_failures(b"[1,2,3]") is None  # not a dict


# ── A4: transient-failure detection (pure) ───────────────────────────────────

class TestLooksTransient:
    def test_network_flake_with_no_structured_failure_is_transient(self):
        assert CIWatcher._looks_transient(
            "curl: (6) Could not resolve host: pypi.org\nConnection reset by peer"
        ) is True

    def test_structured_failure_present_is_not_transient(self):
        # A real assertion alongside a network blip → still a real failure.
        assert CIWatcher._looks_transient("AssertionError: nope\nConnection reset") is False
        assert CIWatcher._looks_transient(
            "=== EVAL FAILURES ===\nscenario failed\ntimed out"
        ) is False

    def test_plain_test_failure_is_not_transient(self):
        assert CIWatcher._looks_transient("tests/x.py::t FAILED\nAssertionError") is False

    def test_empty_is_not_transient(self):
        assert CIWatcher._looks_transient("") is False


# ── A1 + A2: _invoke_coder builds the right brief ────────────────────────────

class TestInvokeCoderBrief:
    def _capture_brief(self, monkeypatch):
        captured = {}

        def fake_run(self, brief, deployment_hint="smart"):
            captured["brief"] = brief
            return CoderOutput(files=[], summary="noop")

        monkeypatch.setattr("app.agents.coder_agent.CoderAgent.run", fake_run)
        return captured

    def test_fetched_files_land_in_target_files_with_repo_map(self, monkeypatch):
        captured = self._capture_brief(monkeypatch)
        fetched = {"backend/app/x.py": "def f():\n    return 1\n"}
        CIWatcher._invoke_coder(_entry(), "FAILBLOB", fetched)
        brief = captured["brief"]
        assert "ci_failure.log" in brief.target_files
        assert brief.target_files["backend/app/x.py"] == fetched["backend/app/x.py"]
        # repo_map built from the fetched file (anti-hallucination context)
        assert brief.repo_map  # non-empty
        # task tells the model to EDIT the real file
        assert "EDIT" in brief.task

    def test_no_fetched_files_degrades_gracefully(self, monkeypatch):
        captured = self._capture_brief(monkeypatch)
        CIWatcher._invoke_coder(_entry(), "FAILBLOB", {})
        brief = captured["brief"]
        assert list(brief.target_files) == ["ci_failure.log"]
        assert brief.repo_map == ""
        assert "could not be auto-fetched" in brief.task

    def test_prior_history_is_fed_into_brief(self, monkeypatch):
        captured = self._capture_brief(monkeypatch)
        entry = _entry()
        entry.history = [
            {"attempt": 1, "files_changed": ["a.py"], "summary": "bumped pin", "patch_hash": "ab"},
        ]
        CIWatcher._invoke_coder(entry, "FAILBLOB", {})
        brief = captured["brief"]
        assert "PRIOR ATTEMPTS THAT DID NOT WORK" in brief.task
        assert "bumped pin" in brief.task
        assert "a.py" in brief.task


# ── A1 + A4: _attempt_fix integration (mocked GitHub + Coder) ─────────────────

class TestAttemptFixIntegration:
    def _wire(self, monkeypatch, *, failure_blob, file_contents):
        """Stub the GitHub surface + Coder so _attempt_fix runs offline."""
        async def fake_head(token, owner, repo, branch):
            return "sha123"

        async def fake_collect(token, owner, repo, status, head_sha=None):
            return failure_blob

        async def fake_get_content(token, owner, repo, path, ref=None):
            return file_contents.get(path)

        commits = []

        async def fake_get_sha(token, owner, repo, path, branch):
            return "filesha"

        async def fake_put(token, owner, repo, path, content, message, branch, sha=None):
            commits.append(path)

        monkeypatch.setattr(GitHubActionsService, "get_branch_head_sha", staticmethod(fake_head))
        monkeypatch.setattr(GitHubActionsService, "collect_failure_context", classmethod(
            lambda cls, *a, **k: fake_collect(*a, **k)))
        monkeypatch.setattr(
            "app.services.ci_watcher.GitHubAPIService.get_file_content",
            staticmethod(fake_get_content),
        )
        monkeypatch.setattr(
            "app.services.ci_watcher.GitHubPRService.get_file_sha", staticmethod(fake_get_sha))
        monkeypatch.setattr(
            "app.services.ci_watcher.GitHubPRService.put_file", staticmethod(fake_put))
        monkeypatch.setattr(CIWatcher, "_touch", classmethod(lambda cls, e, m, level="info": None))
        return commits

    def test_fetches_failing_file_and_passes_to_coder(self, monkeypatch):
        self._wire(
            monkeypatch,
            failure_blob='tests/test_x.py:3: AssertionError\n  File "backend/app/x.py", line 9',
            file_contents={"tests/test_x.py": "def test_x():\n    assert 0\n",
                           "backend/app/x.py": "X=1\n"},
        )
        seen = {}

        def fake_invoke(entry, blob, fetched):
            seen["fetched"] = dict(fetched)
            return CoderOutput(files=[CoderFile(path="backend/app/x.py", new_content="X=2\n", rationale="fix")], summary="fixed")

        monkeypatch.setattr(CIWatcher, "_invoke_coder", staticmethod(fake_invoke))

        status = CheckRunStatus([{"name": "t", "status": "completed", "conclusion": "failure", "id": 1}])
        ok = asyncio.run(CIWatcher._attempt_fix(_entry(), status))
        assert ok is True
        # The dominant fix: the failing files' real contents reached the Coder.
        assert "tests/test_x.py" in seen["fetched"]
        assert "backend/app/x.py" in seen["fetched"]

    def test_transient_failure_reruns_instead_of_patching(self, monkeypatch):
        self._wire(
            monkeypatch,
            failure_blob="curl: Could not resolve host: pypi.org\nConnection reset by peer",
            file_contents={},
        )
        reran = {"called": False}

        async def fake_rerun(token, owner, repo, head_sha):
            reran["called"] = True
            return True

        monkeypatch.setattr(GitHubActionsService, "rerun_failed_jobs", staticmethod(fake_rerun))

        def fake_invoke(entry, blob, fetched):
            raise AssertionError("should NOT patch a transient flake on the first cycle")

        monkeypatch.setattr(CIWatcher, "_invoke_coder", staticmethod(fake_invoke))

        entry = _entry()
        status = CheckRunStatus([{"name": "t", "status": "completed", "conclusion": "failure", "id": 1}])
        ok = asyncio.run(CIWatcher._attempt_fix(entry, status))
        assert ok is True            # loop continues (re-await CI)
        assert reran["called"] is True
        assert entry.reran is True
        assert entry.attempts == 0   # the rerun did NOT consume a real attempt

    def test_real_failure_does_not_rerun(self, monkeypatch):
        self._wire(
            monkeypatch,
            failure_blob="tests/x.py::t FAILED\nAssertionError: boom",
            file_contents={"tests/x.py": "def t():\n    assert 0\n"},
        )

        async def fake_rerun(token, owner, repo, head_sha):
            raise AssertionError("should NOT rerun a real assertion failure")

        monkeypatch.setattr(GitHubActionsService, "rerun_failed_jobs", staticmethod(fake_rerun))
        monkeypatch.setattr(CIWatcher, "_invoke_coder", staticmethod(
            lambda entry, blob, fetched: CoderOutput(
                files=[CoderFile(path="tests/x.py", new_content="def t():\n    assert 1\n", rationale="r")],
                summary="fixed")))

        entry = _entry()
        status = CheckRunStatus([{"name": "t", "status": "completed", "conclusion": "failure", "id": 1}])
        ok = asyncio.run(CIWatcher._attempt_fix(entry, status))
        assert ok is True
        assert entry.reran is False
        assert entry.attempts == 1   # a real fix attempt was spent
