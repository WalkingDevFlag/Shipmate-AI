"""
GitHub Actions read helpers for the CI feedback loop.

Used by CIWatcher to poll the status of a Coder-opened PR and fetch the
failing job logs when CI breaks. All operations work under the existing
`repo` OAuth scope — reading Actions/Checks does NOT need the `workflow`
scope (only writing files under .github/workflows/ does).

Endpoints used:
  GET /repos/{o}/{r}/commits/{sha}/check-runs        — list checks for a SHA
  GET /repos/{o}/{r}/actions/jobs/{job_id}            — job + step metadata
  GET /repos/{o}/{r}/actions/jobs/{job_id}/logs       — 302 -> S3 plaintext
  GET /repos/{o}/{r}/check-runs/{id}/annotations      — structured fallback
  POST /repos/{o}/{r}/issues/{pr}/comments            — escalation comment
  GET /repos/{o}/{r}/git/ref/heads/{branch}           — current branch SHA

Logs are typically 50KB-2MB. We truncate to the last ~30k chars before
feeding back into Coder so we stay under Sonnet's context with headroom
(failures are at the tail of the log).
"""

from __future__ import annotations

import io
import json
import logging
import re
import xml.etree.ElementTree as ET
import zipfile
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger("shipmate.github_actions")

_BASE = "https://api.github.com"
_TIMEOUT = httpx.Timeout(30.0)
_LOG_TAIL_CHARS = 30_000

# The artifact name the CI test-gate uploads (see .github/workflows/ci.yml,
# job backend-test → "Upload test reports"). The zip contains reports/junit.xml.
_TEST_REPORT_ARTIFACT = "backend-test-reports"
_JUNIT_MEMBER_HINT = "junit.xml"        # member filename inside the artifact zip
_JUNIT_MAX_FAILURES = 40                # cap structured failures fed to Coder
_JUNIT_MSG_CHARS = 600                  # truncate each failure message

# The eval-gate uploads structured EvalOps results the same way the test-gate
# uploads JUnit (see .github/workflows/ci.yml). The watcher consumes both.
_EVAL_REPORT_ARTIFACT = "eval-report"
_EVAL_REPORT_MEMBER_HINT = "eval-report.json"
_EVAL_MAX_FAILURES = 30

# How many failing-source paths we extract from a failure blob and re-fetch
# into the Coder's context. Small cap: we want the handful of files the
# failure actually points at, not the whole repo.
_MAX_FAILING_PATHS = 8

# A repo-relative source path in a traceback / pytest header.
# Matches: tests/test_x.py, backend/app/foo.py, src/a/b.ts:42, app\\x.py (win)
_PATH_RE = re.compile(
    r"""(?:File\s+"|^|\s|\(|')           # boundary: File ", line start, space, ( or '
        (?P<path>
            (?:[\w.\-]+[/\\])+            # at least one dir segment
            [\w.\-]+
            # longer extensions first — Python re is leftmost-alternation, so
            # `tsx` must precede `ts` or `App.tsx` truncates to `App.ts`.
            \.(?:tsx|ts|jsx|js|py|go|rb|java|rs|php|cpp|cc|hpp|h|c)
        )
        (?![\w])                          # ext not followed by more word chars
    """,
    re.VERBOSE | re.MULTILINE,
)
# pytest node id header, e.g. "tests/test_x.py::TestA::test_y" or
# "FAILED tests/test_x.py::test_y - AssertionError".
_PYTEST_NODE_RE = re.compile(r"(?P<path>(?:[\w.\-]+[/\\])*[\w.\-]+\.py)::")


def extract_failing_paths(failure_blob: str) -> List[str]:
    """Pull the repo-relative source paths a CI failure points at, so the
    caller can fetch their CURRENT contents and hand them to the Coder as
    real files to edit (instead of forcing a from-scratch rewrite).

    Pure / network-free → unit-testable offline. Sources, in priority order:
      1. pytest node ids  (`tests/test_x.py::test_y`)
      2. traceback / log paths  (`File "app/foo.py", line 12`, `src/a.ts:42`)
      3. JUnit `classname::name` lines from `_format_junit_failures`, where the
         classname is a dotted module (`tests.test_x`) → candidate paths.

    Order-preserving + de-duplicated; capped at `_MAX_FAILING_PATHS`. The
    paths are CANDIDATES — `get_file_content` fails open to None for any that
    don't resolve, so over-matching is harmless.
    """
    if not failure_blob:
        return []

    ordered: List[str] = []
    seen: set[str] = set()

    def _add(p: str) -> None:
        p = p.strip().replace("\\", "/")
        # Strip a trailing :line / :line:col if the regex kept it.
        p = re.sub(r":\d+(?::\d+)?$", "", p)
        if p and p not in seen and not p.startswith(("/", "http")):
            seen.add(p)
            ordered.append(p)

    # 1. pytest node ids — highest signal (real repo-relative path + ::).
    for m in _PYTEST_NODE_RE.finditer(failure_blob):
        _add(m.group("path"))

    # 2. traceback / generic source paths.
    for m in _PATH_RE.finditer(failure_blob):
        _add(m.group("path"))

    # 3. JUnit dotted classnames (the `_format_junit_failures` lines look like
    #    "[FAILURE] tests.test_x::test_y — ..."). Map the dotted module to a
    #    candidate path. Only fires for first-party `app.`/`backend.`/`tests.`.
    for m in re.finditer(r"\[(?:FAILURE|ERROR)\]\s+(?P<cls>[\w.]+)::", failure_blob):
        cls = m.group("cls")
        if "." in cls and "/" not in cls:
            _add(cls.replace(".", "/") + ".py")

    return ordered[:_MAX_FAILING_PATHS]


def _headers(token: str) -> Dict[str, str]:
    # Delegates to the single shared definition (see github_client.gh_headers).
    from app.services.github_client import gh_headers
    return gh_headers(token)


class CheckRunStatus:
    """Aggregate status of all check-runs for a SHA."""

    def __init__(self, runs: List[Dict[str, Any]]) -> None:
        self.runs = runs

    @property
    def all_completed(self) -> bool:
        return all(r.get("status") == "completed" for r in self.runs) and len(self.runs) > 0

    @property
    def any_failed(self) -> bool:
        bad = {"failure", "timed_out", "cancelled", "action_required"}
        return any(r.get("conclusion") in bad for r in self.runs)

    @property
    def failed_runs(self) -> List[Dict[str, Any]]:
        bad = {"failure", "timed_out", "cancelled", "action_required"}
        return [r for r in self.runs if r.get("conclusion") in bad]

    @property
    def all_passed(self) -> bool:
        return self.all_completed and not self.any_failed and len(self.runs) > 0

    @property
    def is_empty(self) -> bool:
        """No checks reported for this SHA — repo has no CI configured."""
        return len(self.runs) == 0


class GitHubActionsService:
    """Async helpers for inspecting GitHub Actions run state on a PR branch."""

    @staticmethod
    async def get_branch_head_sha(token: str, owner: str, repo: str, branch: str) -> str:
        """Current commit SHA at the tip of `branch`. Used to key check-run lookups."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}/repos/{owner}/{repo}/git/ref/heads/{branch}",
                headers=_headers(token),
            )
            resp.raise_for_status()
            return resp.json()["object"]["sha"]

    @staticmethod
    async def list_check_runs(
        token: str, owner: str, repo: str, ref: str,
    ) -> CheckRunStatus:
        """List all check-runs registered against a SHA."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}/repos/{owner}/{repo}/commits/{ref}/check-runs",
                headers=_headers(token),
                params={"per_page": 100},
            )
            resp.raise_for_status()
            return CheckRunStatus(resp.json().get("check_runs", []))

    @staticmethod
    async def fetch_job_logs(
        token: str, owner: str, repo: str, job_id: int,
    ) -> Optional[str]:
        """
        Fetch raw stdout/stderr for a failed Actions job. Returns the LAST
        ~30k characters (failures are at the tail). None if logs are gone
        (cancelled jobs sometimes 404 here — caller should fall back to
        annotations).

        Implementation: GitHub redirects /logs to a signed S3 URL. httpx
        follows redirects automatically when follow_redirects=True.
        """
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True) as client:
            resp = await client.get(
                f"{_BASE}/repos/{owner}/{repo}/actions/jobs/{job_id}/logs",
                headers=_headers(token),
            )
            if resp.status_code == 404:
                logger.info("Job %s logs unavailable (404) — likely cancelled", job_id)
                return None
            if resp.status_code >= 400:
                logger.warning(
                    "Job %s logs HTTP %s: %s",
                    job_id, resp.status_code, resp.text[:200],
                )
                return None
            text = resp.text or ""
            if len(text) > _LOG_TAIL_CHARS:
                head = f"... [log truncated; {len(text) - _LOG_TAIL_CHARS} chars omitted before this tail]\n\n"
                return head + text[-_LOG_TAIL_CHARS:]
            return text

    @staticmethod
    async def fetch_job_annotations(
        token: str, owner: str, repo: str, check_run_id: int,
    ) -> List[Dict[str, Any]]:
        """Structured failure annotations — fallback when raw logs are unavailable."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}/repos/{owner}/{repo}/check-runs/{check_run_id}/annotations",
                headers=_headers(token),
            )
            if resp.status_code >= 400:
                return []
            return resp.json() or []

    @staticmethod
    async def post_pr_comment(
        token: str, owner: str, repo: str, pr_number: int, body: str,
    ) -> None:
        """Post a comment to a PR — used for escalation when watcher gives up."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.post(
                f"{_BASE}/repos/{owner}/{repo}/issues/{pr_number}/comments",
                headers=_headers(token),
                json={"body": body},
            )
            if resp.status_code >= 400:
                logger.warning(
                    "Failed to post escalation comment on %s/%s#%s: %s",
                    owner, repo, pr_number, resp.text[:200],
                )

    @classmethod
    async def rerun_failed_jobs(
        cls, token: str, owner: str, repo: str, head_sha: str,
    ) -> bool:
        """Re-run only the failed jobs of the workflow run at *head_sha* (used
        by the flaky-vs-real guard: a transient failure that passes on rerun
        shouldn't burn a Coder fix attempt). Returns True if the rerun was
        accepted. Best-effort — never raises into the watch loop."""
        try:
            run_id = await cls._find_workflow_run_id(token, owner, repo, head_sha)
            if run_id is None:
                return False
            async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
                resp = await client.post(
                    f"{_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}/rerun-failed-jobs",
                    headers=_headers(token),
                )
                # 201 Created on success; 403 if re-run not permitted on this run.
                if resp.status_code >= 400:
                    logger.info(
                        "rerun_failed_jobs HTTP %s for run %s: %s",
                        resp.status_code, run_id, resp.text[:200],
                    )
                    return False
                return True
        except Exception as e:
            logger.info("rerun_failed_jobs soft-failed: %s", e)
            return False

    # ── Structured test failures (JUnit XML artifact) ───────────────────

    @staticmethod
    async def _find_workflow_run_id(
        token: str, owner: str, repo: str, head_sha: str,
    ) -> Optional[int]:
        """The workflow-run id whose HEAD is *head_sha*. Artifacts hang off
        the run, not the check-run, so we resolve the run first."""
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            resp = await client.get(
                f"{_BASE}/repos/{owner}/{repo}/actions/runs",
                headers=_headers(token),
                params={"head_sha": head_sha, "per_page": 20},
            )
            if resp.status_code >= 400:
                return None
            runs = resp.json().get("workflow_runs", [])
            # Prefer a completed run; fall back to the most recent.
            for r in runs:
                if r.get("status") == "completed":
                    return r.get("id")
            return runs[0].get("id") if runs else None

    @classmethod
    async def _download_artifact_zip(
        cls, token: str, owner: str, repo: str, head_sha: str, artifact_name: str,
    ) -> Optional[bytes]:
        """Resolve the workflow run at *head_sha*, find the named artifact, and
        return its zip bytes. None when the run/artifact is absent or expired.
        Shared by the JUnit and eval-report fetchers."""
        run_id = await cls._find_workflow_run_id(token, owner, repo, head_sha)
        if run_id is None:
            return None
        async with httpx.AsyncClient(timeout=httpx.Timeout(60.0), follow_redirects=True) as client:
            listing = await client.get(
                f"{_BASE}/repos/{owner}/{repo}/actions/runs/{run_id}/artifacts",
                headers=_headers(token),
            )
            if listing.status_code >= 400:
                return None
            artifacts = listing.json().get("artifacts", [])
            target = next(
                (a for a in artifacts if a.get("name") == artifact_name),
                None,
            )
            if target is None or target.get("expired"):
                return None
            dl = await client.get(
                f"{_BASE}/repos/{owner}/{repo}/actions/artifacts/{target['id']}/zip",
                headers=_headers(token),
            )
            if dl.status_code >= 400:
                return None
            return dl.content

    @classmethod
    async def fetch_junit_failures(
        cls, token: str, owner: str, repo: str, head_sha: str,
    ) -> Optional[str]:
        """
        Download the `backend-test-reports` artifact for the run at *head_sha*,
        unzip it in memory, parse the JUnit XML, and return a compact
        plaintext list of FAILED/ERRORED testcases:

            tests/test_x.py::TestA::test_y — AssertionError: expected 200 got 422

        This is structured failure data (test id + file + exception) that the
        Coder fix pass can act on directly, instead of grepping 30k chars of
        raw pytest stdout. Returns None when the artifact is absent (e.g. the
        run failed before upload, or the repo predates the JUnit-emitting CI).
        """
        try:
            zip_bytes = await cls._download_artifact_zip(
                token, owner, repo, head_sha, _TEST_REPORT_ARTIFACT,
            )
            if zip_bytes is None:
                return None
            return cls._parse_junit_zip(zip_bytes)
        except Exception as e:  # never let artifact issues break the fix loop
            logger.info("fetch_junit_failures soft-failed: %s", e)
            return None

    @classmethod
    async def fetch_eval_failures(
        cls, token: str, owner: str, repo: str, head_sha: str,
    ) -> Optional[str]:
        """
        Download the `eval-report` artifact (EvalOps integration-gate output)
        for the run at *head_sha*, parse the JSON, and return a compact list of
        failing scenarios / unsatisfied metrics. This is the highest-signal
        failure context for an eval-gate red — scenario X returned 422 not 200,
        metric Y never fired — far more actionable than raw logs. None when the
        artifact is absent (most repos / non-eval failures).
        """
        try:
            zip_bytes = await cls._download_artifact_zip(
                token, owner, repo, head_sha, _EVAL_REPORT_ARTIFACT,
            )
            if zip_bytes is None:
                return None
            return cls._parse_eval_zip(zip_bytes)
        except Exception as e:  # never let artifact issues break the fix loop
            logger.info("fetch_eval_failures soft-failed: %s", e)
            return None

    @classmethod
    def _parse_junit_zip(cls, zip_bytes: bytes) -> Optional[str]:
        """Extract junit.xml from the artifact zip and format its failures.
        Pure/synchronous so it's unit-testable without the network."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile:
            return None
        member = next(
            (n for n in zf.namelist() if n.endswith(_JUNIT_MEMBER_HINT)),
            None,
        )
        if member is None:
            return None
        xml_bytes = zf.read(member)
        return cls._format_junit_failures(xml_bytes)

    @classmethod
    def _format_junit_failures(cls, xml_bytes: bytes) -> Optional[str]:
        """Turn JUnit XML bytes into a compact failure list. None if it
        doesn't parse or there are no failures/errors."""
        try:
            root = ET.fromstring(xml_bytes)
        except ET.ParseError:
            return None

        lines: List[str] = []
        # JUnit nests <testcase> under <testsuite>(s); iter() finds them at any depth.
        for case in root.iter("testcase"):
            problem = None
            for tag in ("failure", "error"):
                node = case.find(tag)
                if node is not None:
                    problem = (tag, node)
                    break
            if problem is None:
                continue

            tag, node = problem
            classname = case.get("classname", "")
            name = case.get("name", "?")
            test_id = f"{classname}::{name}" if classname else name
            # message attr is the short reason; text is the full traceback.
            msg = (node.get("message") or (node.text or "").strip() or tag)
            msg = " ".join(msg.split())  # collapse whitespace/newlines
            if len(msg) > _JUNIT_MSG_CHARS:
                msg = msg[:_JUNIT_MSG_CHARS] + " …[truncated]"
            lines.append(f"  [{tag.upper()}] {test_id} — {msg}")
            if len(lines) >= _JUNIT_MAX_FAILURES:
                lines.append(f"  …[{_JUNIT_MAX_FAILURES}+ failures; list truncated]")
                break

        if not lines:
            return None
        return "Parsed JUnit test failures (test id — reason):\n" + "\n".join(lines)

    @classmethod
    def _parse_eval_zip(cls, zip_bytes: bytes) -> Optional[str]:
        """Extract eval-report.json from the artifact zip and format its
        failures. Pure/synchronous so it's unit-testable without the network."""
        try:
            zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
        except zipfile.BadZipFile:
            return None
        member = next(
            (n for n in zf.namelist() if n.endswith(_EVAL_REPORT_MEMBER_HINT)),
            None,
        )
        if member is None:
            return None
        return cls._format_eval_failures(zf.read(member))

    @classmethod
    def _format_eval_failures(cls, json_bytes: bytes) -> Optional[str]:
        """Turn an eval-report.json into a compact failure list: failing
        scenarios (with status code + reasons), unsatisfied metrics, and log
        violations. None if it parses clean / has no failures. Parses the JSON
        directly (not via the Pydantic model) to stay robust to schema drift,
        mirroring how _format_junit_failures reads XML directly."""
        try:
            report = json.loads(json_bytes)
        except (json.JSONDecodeError, ValueError):
            return None
        if not isinstance(report, dict):
            return None

        lines: List[str] = []
        top_error = report.get("error")
        if top_error:
            lines.append(f"  [EVAL DID NOT RUN] {str(top_error)[:_JUNIT_MSG_CHARS]}")

        for scn in report.get("scenarios", []) or []:
            if isinstance(scn, dict) and not scn.get("passed", True):
                name = scn.get("name", "?")
                code = scn.get("status_code")
                reasons = "; ".join(str(f) for f in (scn.get("failures") or []))
                reasons = " ".join(reasons.split())
                if len(reasons) > _JUNIT_MSG_CHARS:
                    reasons = reasons[:_JUNIT_MSG_CHARS] + " …[truncated]"
                code_str = f" (HTTP {code})" if code is not None else ""
                lines.append(f"  [SCENARIO FAILED] {name}{code_str} — {reasons or 'assertion failed'}")
            if len(lines) >= _EVAL_MAX_FAILURES:
                break

        for met in report.get("metrics", []) or []:
            if isinstance(met, dict) and not met.get("satisfied", True):
                name = met.get("name", "?")
                state = "never fired" if not met.get("fired") else "fired unexpectedly"
                lines.append(f"  [METRIC UNSATISFIED] {name} — {state}")
            if len(lines) >= _EVAL_MAX_FAILURES:
                break

        for viol in (report.get("log_violations") or [])[:5]:
            lines.append(f"  [LOG VIOLATION] {str(viol)[:200]}")

        if not lines:
            return None
        return "Parsed EvalOps failures (scenario / metric — reason):\n" + "\n".join(lines)

    @classmethod
    async def collect_failure_context(
        cls, token: str, owner: str, repo: str, status: CheckRunStatus,
        head_sha: Optional[str] = None,
    ) -> str:
        """
        Build a plaintext failure-context blob to feed back into Coder.
        Pulls logs (or annotations as fallback) for every failed check-run,
        and — when *head_sha* is given — prepends the STRUCTURED JUnit test
        failures parsed from the uploaded test-report artifact. The structured
        list goes first because it's the highest-signal context for the fix.

        Caller should wrap this string in a CoderBrief.target_files entry
        keyed e.g. "ci_failure.log".
        """
        chunks: List[str] = []

        if head_sha:
            # EvalOps failures first — highest-signal (behavioural: scenario X
            # returned 422, metric Y never fired). Then JUnit. Then raw logs.
            eval_fail = await cls.fetch_eval_failures(token, owner, repo, head_sha)
            if eval_fail:
                chunks.append("=== EVAL FAILURES ===")
                chunks.append(eval_fail)
                chunks.append("")
            junit = await cls.fetch_junit_failures(token, owner, repo, head_sha)
            if junit:
                chunks.append("=== STRUCTURED TEST FAILURES (JUnit) ===")
                chunks.append(junit)
                chunks.append("")

        for run in status.failed_runs:
            check_id = run.get("id")
            job_id = run.get("id")  # for Actions, check_run id == job id
            name = run.get("name", "?")
            conclusion = run.get("conclusion", "?")
            chunks.append(f"=== check-run: {name}  (conclusion: {conclusion}) ===")

            log = None
            if isinstance(job_id, int):
                log = await cls.fetch_job_logs(token, owner, repo, job_id)
            if log:
                chunks.append(log)
            else:
                # Fallback: structured annotations
                if isinstance(check_id, int):
                    anns = await cls.fetch_job_annotations(token, owner, repo, check_id)
                    if anns:
                        chunks.append("(no raw logs; using annotations)")
                        for a in anns[:20]:
                            chunks.append(
                                f"  [{a.get('annotation_level','?')}] "
                                f"{a.get('path','?')}:{a.get('start_line','?')} — "
                                f"{a.get('message','')[:300]}"
                            )
                    else:
                        chunks.append("(no logs and no annotations available)")
                else:
                    chunks.append("(no logs available)")
            chunks.append("")  # blank line between checks

        return "\n".join(chunks) if chunks else "(no failure context available)"
