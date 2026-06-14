"""
CIWatcher — closed-loop CI feedback for Coder-opened PRs.

Flow per registered PR:
  1. Wait for CI to land at HEAD of the PR's branch (poll every 30s, then
     backoff if it lingers).
  2. If all checks pass → drop the registry entry, done.
  3. If any check failed → fetch logs, build a CoderBrief carrying the
     failure context + the original finding + the current file contents
     of the failing-area, ask Coder for a corrective patch, commit it
     to the SAME branch (pushes auto-trigger CI again).
  4. Loop. Cap at 3 attempts, hard 45min wall-clock per PR. Bail early on
     identical-patch hash (Coder is hallucinating in a loop).
  5. On cap exhaustion → post a PR comment escalating to a human.

State-of-record is sqlite (InflightRegistry.ci_watch_state) so an open PR's
watch survives a backend restart — uvicorn --reload fires on every code
change in dev, and without persistence each reload abandoned in-flight PRs.
The asyncio.Task handles still live in the in-process `_tasks` dict (you
can't pickle a coroutine), but the durable facts (status, attempts, finding,
token) round-trip through sqlite. On startup, `resume_from_db()` re-spawns
supervisors for any row still in a non-terminal state.

Supervisor runs as an asyncio.Task, so /api/actuate latency is unchanged
(the orchestrator returns immediately after register()).
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Tuple

from app.agents.coder_agent import CoderAgent, CoderBrief, CoderOutput
from app.schemas.api_schemas import FindingPayload, RepoLensSummary
from app.services import inflight_registry as ir
from app.services import session_store
from app.services import repo_map
from app.services.github_actions_service import (
    CheckRunStatus, GitHubActionsService, extract_failing_paths,
)
from app.services.github_api_service import GitHubAPIService
from app.services.github_pr_service import GitHubPRService

logger = logging.getLogger("shipmate.ci_watcher")

# ── Tunables ────────────────────────────────────────────────────────────────
MAX_ATTEMPTS = 3
TOTAL_WALL_CLOCK_LIMIT_S = 45 * 60          # bail after 45min regardless
INITIAL_POLL_INTERVAL_S = 30
MAX_POLL_INTERVAL_S = 300
POLL_BACKOFF_AFTER_N = 10                   # # of polls before linear backoff
ATTEMPT_BACKOFF_S = (0, 60, 180)            # gap between fix attempts


@dataclass
class WatchEntry:
    owner: str
    repo: str
    pr_number: int
    branch: str
    base_branch: str
    access_token: str
    finding: FindingPayload
    repo_lens: Optional[RepoLensSummary]
    started_at: float = field(default_factory=time.monotonic)
    attempts: int = 0
    history: List[Dict[str, Any]] = field(default_factory=list)
    last_patch_hash: Optional[str] = None
    reran: bool = False  # flaky guard: at most one rerun-failed-jobs before patching
    status: str = "watching"  # watching | fixing | passed | gave_up | crashed
    last_error: Optional[str] = None
    last_event_at: float = field(default_factory=time.monotonic)
    pr_url: Optional[str] = None

    def key(self) -> Tuple[str, str, int]:
        return (self.owner, self.repo, self.pr_number)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "owner": self.owner,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "pr_url": self.pr_url,
            "branch": self.branch,
            "status": self.status,
            "attempts": self.attempts,
            "max_attempts": MAX_ATTEMPTS,
            "history": self.history,
            "last_error": self.last_error,
            "started_ago_s": int(time.monotonic() - self.started_at),
            "last_event_ago_s": int(time.monotonic() - self.last_event_at),
            "finding_id": self.finding.id,
            "finding_title": self.finding.title,
        }


class CIWatcher:
    """Singleton manager — keyed by (owner, repo, pr_number)."""

    _registry: Dict[Tuple[str, str, int], WatchEntry] = {}
    _tasks: Dict[Tuple[str, str, int], asyncio.Task] = {}

    # ── Public API ──────────────────────────────────────────────────────

    @classmethod
    def register(
        cls,
        *,
        owner: str,
        repo: str,
        pr_number: int,
        pr_url: str,
        branch: str,
        base_branch: str,
        access_token: str,
        finding: FindingPayload,
        repo_lens: Optional[RepoLensSummary],
    ) -> None:
        """
        Register a freshly-opened PR for CI watching. Spawns a background
        task that owns its lifecycle. Idempotent — calling twice cancels
        the prior task and starts fresh.
        """
        entry = WatchEntry(
            owner=owner, repo=repo, pr_number=pr_number, branch=branch,
            base_branch=base_branch, access_token=access_token,
            finding=finding, repo_lens=repo_lens, pr_url=pr_url,
        )
        key = entry.key()
        # Cancel prior watcher for the same PR if present.
        if key in cls._tasks:
            cls._tasks[key].cancel()
        cls._registry[key] = entry
        cls._persist(entry)  # durable state-of-record before the task starts
        # Bind the asyncio task so it stays alive even after register() returns.
        cls._tasks[key] = asyncio.create_task(
            cls._supervise(entry), name=f"ci-watch-{owner}-{repo}-{pr_number}",
        )
        logger.info(
            "CIWatcher.register: %s/%s#%s on branch %s (finding=%s)",
            owner, repo, pr_number, branch, finding.id,
        )

    @classmethod
    def list_active(cls) -> List[Dict[str, Any]]:
        """Live in-process entries first; fall back to sqlite for rows whose
        supervisor task isn't running in THIS process (e.g. right after a
        restart before resume_from_db, or watches started by the CLI loop)."""
        live = {e.key(): e.to_dict() for e in cls._registry.values()}
        try:
            for row in ir.list_ci_watches():
                key = (row["owner"], row["repo"], row["pr_number"])
                if key not in live:
                    live[key] = cls._row_to_dict(row)
        except Exception as e:
            logger.debug("list_active: sqlite read failed: %s", e)
        return list(live.values())

    @classmethod
    def get(cls, owner: str, repo: str, pr_number: int) -> Optional[Dict[str, Any]]:
        e = cls._registry.get((owner, repo, pr_number))
        if e:
            return e.to_dict()
        try:
            row = ir.get_ci_watch(owner, repo, pr_number)
            return cls._row_to_dict(row) if row else None
        except Exception:
            return None

    @classmethod
    def stop(cls, owner: str, repo: str, pr_number: int) -> bool:
        key = (owner, repo, pr_number)
        task = cls._tasks.pop(key, None)
        cls._registry.pop(key, None)
        try:
            ir.delete_ci_watch(owner, repo, pr_number)
        except Exception as e:
            logger.debug("stop: sqlite delete failed: %s", e)
        if task and not task.done():
            task.cancel()
            return True
        return False

    # ── Persistence ─────────────────────────────────────────────────────

    @classmethod
    def _persist(cls, entry: WatchEntry) -> None:
        """Mirror the entry's durable facts to sqlite. Best-effort — a
        persistence failure must never break the watch loop itself.

        We persist a VAULT SESSION ID, never the raw token: the ci_watch_state
        row needs *a way to get* a token after a restart, but the token itself
        now lives only in the session vault. session_store.ensure() is
        idempotent, so re-persisting on every poll reuses the same session."""
        try:
            session_ref = session_store.ensure(entry.access_token)
        except Exception as e:  # vault hiccup — persist without a token ref
            logger.debug("_persist: session ensure failed for %s: %s", entry.key(), e)
            session_ref = None
        try:
            ir.upsert_ci_watch(
                entry.owner, entry.repo, entry.pr_number,
                pr_url=entry.pr_url,
                branch=entry.branch,
                base_branch=entry.base_branch,
                finding=entry.finding,
                repo_lens=entry.repo_lens,
                access_token=session_ref,   # session id, NOT the raw token
                status=entry.status,
                attempts=entry.attempts,
                last_patch_hash=entry.last_patch_hash,
                last_error=entry.last_error,
            )
        except Exception as e:
            logger.debug("_persist failed for %s: %s", entry.key(), e)

    @staticmethod
    def _row_to_dict(row: Dict[str, Any]) -> Dict[str, Any]:
        """Shape a sqlite ci_watch_state row like WatchEntry.to_dict() so the
        /api/watcher consumers don't care whether it came from memory or disk."""
        finding = row.get("finding") or {}
        return {
            "owner": row["owner"],
            "repo": row["repo"],
            "pr_number": row["pr_number"],
            "pr_url": row.get("pr_url"),
            "branch": row.get("branch"),
            "status": row.get("status"),
            "attempts": row.get("attempts", 0),
            "max_attempts": MAX_ATTEMPTS,
            "history": [],  # history is in ci_watch_log, fetched separately
            "last_error": row.get("last_error"),
            "started_ago_s": None,  # monotonic clock doesn't survive restart
            "last_event_ago_s": None,
            "finding_id": finding.get("id"),
            "finding_title": finding.get("title"),
            "persisted": True,
        }

    @classmethod
    def resume_from_db(cls) -> int:
        """Re-spawn supervisor tasks for rows still in a non-terminal state.
        Called from the FastAPI lifespan startup hook. Returns the number of
        watchers resumed. Rows missing an access_token can't be polled, so
        they're marked crashed instead of silently abandoned."""
        resumed = 0
        try:
            rows = ir.list_ci_watches(status_in=["watching", "fixing"])
        except Exception as e:
            logger.warning("resume_from_db: sqlite read failed: %s", e)
            return 0

        for row in rows:
            key = (row["owner"], row["repo"], row["pr_number"])
            if key in cls._tasks:
                continue  # already running in this process
            # The stored access_token column holds a vault SESSION ID — resolve
            # it back to a real token. A raw token (legacy row) passes through.
            stored = row.get("access_token")
            token = session_store.resolve(stored) if session_store.looks_like_session(stored) else stored
            if not token:
                ir.upsert_ci_watch(
                    row["owner"], row["repo"], row["pr_number"],
                    status="crashed",
                    last_error="session expired/unresolvable — cannot resume after restart",
                )
                continue
            try:
                finding = FindingPayload(**(row["finding"] or {}))
                repo_lens = (
                    RepoLensSummary(**row["repo_lens"]) if row.get("repo_lens") else None
                )
            except Exception as e:
                logger.warning("resume_from_db: bad row %s: %s", key, e)
                continue
            entry = WatchEntry(
                owner=row["owner"], repo=row["repo"], pr_number=row["pr_number"],
                branch=row["branch"], base_branch=row["base_branch"],
                access_token=token, finding=finding, repo_lens=repo_lens,
                pr_url=row.get("pr_url"), attempts=row.get("attempts", 0),
                last_patch_hash=row.get("last_patch_hash"),
                status=row.get("status", "watching"),
            )
            cls._registry[key] = entry
            cls._tasks[key] = asyncio.create_task(
                cls._supervise(entry),
                name=f"ci-watch-resume-{key[0]}-{key[1]}-{key[2]}",
            )
            ir.append_log(key[0], key[1], key[2], "info",
                          "resumed watcher after backend restart")
            resumed += 1
        if resumed:
            logger.info("CIWatcher.resume_from_db: resumed %d watcher(s)", resumed)
        return resumed

    # ── Supervisor loop ─────────────────────────────────────────────────

    @classmethod
    async def _supervise(cls, entry: WatchEntry) -> None:
        """Owns the watcher lifecycle — runs until terminal state."""
        try:
            while True:
                # Hard wall-clock guard.
                if time.monotonic() - entry.started_at > TOTAL_WALL_CLOCK_LIMIT_S:
                    logger.warning(
                        "CIWatcher: %s/%s#%s exceeded %ds wall-clock; giving up",
                        entry.owner, entry.repo, entry.pr_number, TOTAL_WALL_CLOCK_LIMIT_S,
                    )
                    entry.status = "gave_up"
                    entry.last_error = "wall-clock limit reached"
                    await cls._escalate(entry, reason="wall-clock limit reached (45min)")
                    return

                # Wait for CI to land.
                status = await cls._await_ci(entry)
                if status is None:
                    # Watcher cancelled or fatal error — _await_ci logged it.
                    return

                # No checks at all? Repo has no CI. Drop quietly.
                if status.is_empty:
                    logger.info(
                        "CIWatcher: %s/%s#%s has no check-runs (no CI configured); dropping",
                        entry.owner, entry.repo, entry.pr_number,
                    )
                    entry.status = "passed"  # nothing to fail
                    cls._touch(entry, "no-ci")
                    return

                if status.all_passed:
                    logger.info(
                        "CIWatcher: %s/%s#%s CI passed after %d attempt(s); done",
                        entry.owner, entry.repo, entry.pr_number, entry.attempts,
                    )
                    entry.status = "passed"
                    cls._touch(entry, "passed")
                    if entry.attempts > 0:
                        await GitHubActionsService.post_pr_comment(
                            entry.access_token, entry.owner, entry.repo, entry.pr_number,
                            f"✅ ShipMate auto-fixed CI in {entry.attempts} attempt(s).",
                        )
                    return

                # CI failed. Try a fix?
                if entry.attempts >= MAX_ATTEMPTS:
                    logger.warning(
                        "CIWatcher: %s/%s#%s exhausted %d attempts; escalating",
                        entry.owner, entry.repo, entry.pr_number, MAX_ATTEMPTS,
                    )
                    entry.status = "gave_up"
                    entry.last_error = "max attempts exhausted"
                    await cls._escalate(entry, reason=f"exhausted {MAX_ATTEMPTS} auto-fix attempts")
                    return

                # Backoff before this attempt.
                gap = ATTEMPT_BACKOFF_S[min(entry.attempts, len(ATTEMPT_BACKOFF_S) - 1)]
                if gap > 0:
                    await asyncio.sleep(gap)

                ok = await cls._attempt_fix(entry, status)
                if not ok:
                    # Fix step crashed (Bedrock failure, GitHub write rejected,
                    # identical patch). _attempt_fix logged + updated entry.
                    return
                # Fix committed; loop back to await_ci on the new SHA.

        except asyncio.CancelledError:
            logger.info("CIWatcher: %s/%s#%s cancelled", entry.owner, entry.repo, entry.pr_number)
            raise
        except Exception as e:
            logger.exception("CIWatcher: %s/%s#%s supervisor crashed: %s",
                             entry.owner, entry.repo, entry.pr_number, e)
            entry.status = "crashed"
            entry.last_error = str(e)
        finally:
            # Always remove the task ref; entry stays for status queries.
            cls._tasks.pop(entry.key(), None)

    @classmethod
    async def _await_ci(cls, entry: WatchEntry) -> Optional[CheckRunStatus]:
        """Poll until all check-runs are completed. None on cancel/fatal."""
        polls = 0
        while True:
            try:
                head_sha = await GitHubActionsService.get_branch_head_sha(
                    entry.access_token, entry.owner, entry.repo, entry.branch,
                )
                status = await GitHubActionsService.list_check_runs(
                    entry.access_token, entry.owner, entry.repo, head_sha,
                )
            except Exception as e:
                # OAuth expired / repo deleted / network. Bail loudly.
                logger.warning(
                    "CIWatcher: %s/%s#%s GitHub poll failed: %s",
                    entry.owner, entry.repo, entry.pr_number, e,
                )
                entry.status = "crashed"
                entry.last_error = f"github poll: {e}"
                return None

            polls += 1
            cls._touch(
                entry,
                f"poll[{polls}] runs={len(status.runs)} "
                f"completed={status.all_completed} any_failed={status.any_failed}",
            )

            if status.is_empty and polls >= 6:
                # 3 minutes of empty checks — repo has no CI at all.
                return status
            if status.all_completed:
                return status

            # Act on a KNOWN failure without waiting for slow/stuck siblings.
            # A single failed check means CI is red regardless of what the
            # pending jobs do, and the Coder fix + new commit re-triggers the
            # whole run anyway. Critically, this unblocks the case where one
            # job is wedged in GitHub's infra (queued/stuck in_progress for
            # tens of minutes): without this, all_completed never goes true and
            # the watcher would idle until the 45-min wall clock, never fixing
            # the failure it can already see. We require a brief settle (a
            # couple polls) so we don't fire on a transient first-poll blip
            # before fast checks register.
            if status.any_failed and polls >= 2:
                logger.info(
                    "CIWatcher: %s/%s#%s has a failed check with %d run(s) still "
                    "pending — acting now rather than waiting for stragglers",
                    entry.owner, entry.repo, entry.pr_number,
                    sum(1 for r in status.runs if r.get("status") != "completed"),
                )
                return status

            # Wall-clock check inside the wait loop too.
            if time.monotonic() - entry.started_at > TOTAL_WALL_CLOCK_LIMIT_S:
                return status  # let _supervise handle the gave-up path

            interval = INITIAL_POLL_INTERVAL_S
            if polls > POLL_BACKOFF_AFTER_N:
                # Linear ramp: 30s -> 60s -> 90s ... capped at MAX_POLL_INTERVAL_S
                interval = min(MAX_POLL_INTERVAL_S, INITIAL_POLL_INTERVAL_S + 30 * (polls - POLL_BACKOFF_AFTER_N))
            await asyncio.sleep(interval)

    @classmethod
    async def _attempt_fix(cls, entry: WatchEntry, status: CheckRunStatus) -> bool:
        """
        Pull failure logs, ask Coder for a fix, commit it to the branch.
        Returns True if a fix was committed (loop should continue), False
        if we should stop (identical patch / Bedrock failure / commit failed).
        """
        entry.status = "fixing"
        entry.attempts += 1
        attempt_idx = entry.attempts
        cls._touch(entry, f"attempt[{attempt_idx}] starting")

        # Fetch failure context. Resolve the current HEAD SHA so
        # collect_failure_context can also pull the STRUCTURED JUnit test
        # failures from the uploaded test-report artifact (highest-signal
        # input for the fix). SHA resolution is best-effort — on failure we
        # still get raw logs.
        head_sha: Optional[str] = None
        try:
            head_sha = await GitHubActionsService.get_branch_head_sha(
                entry.access_token, entry.owner, entry.repo, entry.branch,
            )
        except Exception as e:
            logger.info("CIWatcher: head-sha resolve failed (JUnit skipped): %s", e)

        try:
            failure_blob = await GitHubActionsService.collect_failure_context(
                entry.access_token, entry.owner, entry.repo, status,
                head_sha=head_sha,
            )
        except Exception as e:
            logger.warning("CIWatcher: failure-log fetch failed: %s", e)
            entry.last_error = f"log fetch: {e}"
            await cls._escalate(entry, reason=f"could not fetch CI logs: {e}")
            entry.status = "gave_up"
            return False

        # Flaky-vs-real guard (A4): if this looks like a transient infra flake
        # (network/timeout/runner blip) AND there's no structured test/eval
        # failure to act on, re-run the failed jobs ONCE rather than burning a
        # Coder attempt on correct code. A real failure re-fails and we patch
        # it next cycle; a flake goes green. Gated to one rerun via entry.reran.
        if not entry.reran and head_sha and cls._looks_transient(failure_blob):
            entry.reran = True
            entry.attempts -= 1  # this cycle didn't spend a real fix attempt
            reran = await GitHubActionsService.rerun_failed_jobs(
                entry.access_token, entry.owner, entry.repo, head_sha,
            )
            if reran:
                entry.status = "watching"
                cls._touch(
                    entry,
                    "transient-looking failure (no structured failures) — "
                    "re-ran failed jobs once before patching",
                )
                return True  # loop back to _await_ci on the rerun
            # Rerun not accepted — fall through and patch as normal.
            entry.attempts += 1

        # Fetch the CURRENT contents of the files the failure points at, so
        # Coder edits real files instead of hallucinating a from-scratch
        # rewrite. This is the single biggest reliability lever for the loop:
        # before this, target_files was {"ci_failure.log": ...} ONLY. Best-
        # effort and fail-open — get_file_content returns None for anything
        # that doesn't resolve (over-matched path, binary, 404).
        fetched_files: Dict[str, str] = {}
        try:
            paths = extract_failing_paths(failure_blob)
            for path in paths:
                content = await GitHubAPIService.get_file_content(
                    entry.access_token, entry.owner, entry.repo, path, ref=head_sha,
                )
                if content is not None:
                    fetched_files[path] = content
            if fetched_files:
                cls._touch(
                    entry,
                    f"attempt[{attempt_idx}] fetched {len(fetched_files)} "
                    f"failing-file(s): {', '.join(fetched_files)}",
                )
        except Exception as e:
            # Never let context-enrichment break the fix loop — Coder can still
            # work from the failure blob alone (degraded, pre-fix behaviour).
            logger.info("CIWatcher: failing-file fetch soft-failed: %s", e)

        # Ask Coder to diagnose + patch.
        try:
            coder_out = await asyncio.to_thread(
                cls._invoke_coder, entry, failure_blob, fetched_files,
            )
        except Exception as e:
            logger.warning("CIWatcher: Coder failed during fix attempt: %s", e)
            entry.last_error = f"coder: {e}"
            await cls._escalate(entry, reason=f"Coder agent failed: {e}")
            entry.status = "gave_up"
            return False

        if not coder_out.files:
            logger.info("CIWatcher: Coder returned no files; nothing to commit")
            entry.last_error = "Coder produced no patch"
            await cls._escalate(
                entry,
                reason="Coder agent saw the failure but had no patch to offer.",
            )
            entry.status = "gave_up"
            return False

        # Identical-patch bailout.
        patch_hash = cls._hash_files(coder_out.files)
        if patch_hash == entry.last_patch_hash:
            logger.warning("CIWatcher: identical patch hash %s — Coder is looping; bailing",
                           patch_hash[:10])
            entry.last_error = "Coder produced identical patch twice"
            await cls._escalate(
                entry,
                reason="Auto-fix loop detected — Coder produced the same patch twice. Manual review needed.",
            )
            entry.status = "gave_up"
            return False
        entry.last_patch_hash = patch_hash

        # Commit each file to the existing branch.
        try:
            for cf in coder_out.files:
                existing_sha = await GitHubPRService.get_file_sha(
                    entry.access_token, entry.owner, entry.repo, cf.path, entry.branch,
                )
                await GitHubPRService.put_file(
                    entry.access_token, entry.owner, entry.repo,
                    cf.path, cf.new_content,
                    f"shipmate(ci-fix): attempt {attempt_idx} — {cf.path}",
                    entry.branch, sha=existing_sha,
                )
        except Exception as e:
            logger.warning("CIWatcher: commit failed: %s", e)
            entry.last_error = f"commit: {e}"
            await cls._escalate(entry, reason=f"commit failed: {e}")
            entry.status = "gave_up"
            return False

        entry.history.append({
            "attempt": attempt_idx,
            "files_changed": [cf.path for cf in coder_out.files],
            "summary": coder_out.summary,
            "patch_hash": patch_hash[:10],
        })
        cls._touch(entry, f"attempt[{attempt_idx}] committed {len(coder_out.files)} file(s)")
        return True

    # ── Helpers ─────────────────────────────────────────────────────────

    @staticmethod
    def _invoke_coder(
        entry: WatchEntry,
        failure_blob: str,
        fetched_files: Optional[Dict[str, str]] = None,
    ) -> CoderOutput:
        """Synchronous Coder invocation (runs on worker thread).

        `fetched_files` carries the CURRENT contents of the source files the
        failure points at (resolved by the async caller via
        GitHubAPIService.get_file_content). Passing them in target_files turns
        the fix from "rewrite a file you can't see" into "edit the real file"
        — the dominant reliability fix for the loop."""
        fetched_files = fetched_files or {}
        # The failure blob plus the real failing files. Coder's prompt rule
        # says "produce full new file content"; with the real file present it
        # edits rather than hallucinates.
        target_files: Dict[str, str] = {"ci_failure.log": failure_blob}
        target_files.update(fetched_files)
        ctx = entry.repo_lens or RepoLensSummary()

        # Anti-hallucination context: a repo map scoped to the failing files so
        # imports/symbols in the rewrite stay grounded in real modules. Built
        # from the fetched files (fail-open to "" on a thin tree).
        repo_map_str = ""
        if fetched_files:
            try:
                file_tree = list(fetched_files) + list(ctx.entry_points or [])
                repo_map_str = repo_map.build_repo_map(
                    file_tree, fetched_files, list(fetched_files),
                )
            except Exception as e:  # never let context-building break the fix
                logger.debug("CIWatcher: repo_map build soft-failed: %s", e)

        # A2: feed prior failed attempts back in so retries are INFORMED, not
        # blind. Each history item is recorded after a commit (attempt,
        # files_changed, summary, patch_hash). Without this, attempts 2-3 just
        # re-converge on near-duplicate patches.
        history_block = ""
        if entry.history:
            lines = []
            for h in entry.history:
                files = ", ".join(h.get("files_changed", [])) or "(no files)"
                summary = (h.get("summary") or "").strip()
                lines.append(
                    f"  • Attempt {h.get('attempt')}: edited [{files}] — "
                    f"{summary[:200]} → CI STILL FAILED."
                )
            history_block = (
                "PRIOR ATTEMPTS THAT DID NOT WORK (do NOT repeat these — try a "
                "DIFFERENT fix):\n" + "\n".join(lines) + "\n\n"
            )

        files_hint = (
            (
                "The current contents of the failing file(s) are in "
                f"`target_files`: {', '.join(fetched_files)}. EDIT them — return "
                "the full corrected content for the file(s) you change. "
            )
            if fetched_files
            else (
                "The failing source file(s) could not be auto-fetched; reconstruct "
                "the fix from the failure log and the finding context. "
            )
        )

        brief = CoderBrief(
            task=(
                f"{history_block}"
                f"The previous patch you generated was committed and CI failed. "
                f"Read `target_files['ci_failure.log']`. If it begins with a "
                f"'STRUCTURED TEST FAILURES (JUnit)' or '=== EVAL FAILURES ===' "
                f"section, TRUST THAT FIRST — it pinpoints the regression more "
                f"precisely than the raw log tail below it. {files_hint}"
                f"Then produce a corrective patch. The originating finding was: "
                f"{entry.finding.title}. "
                f"Description: {entry.finding.description}. "
                f"Apply the SMALLEST possible fix that makes CI green. "
                f"Don't refactor unrelated code. If the failure is in a config "
                f"file (linter, requirements, workflow yaml), fix it there. "
                f"If the failure is in your test or source code, fix that. "
                f"This is attempt #{entry.attempts} of {MAX_ATTEMPTS}."
            ),
            repo_full_name=f"{entry.owner}/{entry.repo}",
            primary_language=ctx.primary_language,
            tech_stack=ctx.tech_stack,
            entry_points=ctx.entry_points,
            target_files=target_files,
            repo_map=repo_map_str,
            finding_kind="ci_failure",
            finding_id=f"{entry.finding.id}-fix-{entry.attempts}",
            finding_severity="high",
        )
        return CoderAgent().run(brief, deployment_hint="smart")

    # Substrings that mark a real, deterministic failure — if any are present
    # we do NOT treat the run as flaky (there's something concrete to fix).
    _STRUCTURED_FAILURE_MARKERS = (
        "=== STRUCTURED TEST FAILURES (JUnit) ===",
        "=== EVAL FAILURES ===",
        "AssertionError",
        "SyntaxError",
        "ImportError",
        "ModuleNotFoundError",
        "NameError",
        "TypeError",
        "FAILED ",
    )
    # Substrings that suggest a transient infra/runner flake rather than a code bug.
    _TRANSIENT_MARKERS = (
        "connection reset",
        "connection refused",
        "timed out",
        "timeout",
        "temporary failure in name resolution",
        "could not resolve host",
        "503 server error",
        "502 bad gateway",
        "429 too many requests",
        "network is unreachable",
        "tls handshake",
        "runner has received a shutdown signal",
        "the runner has received a shutdown",
        "lost communication with the server",
        "econnreset",
        "etimedout",
    )

    @classmethod
    def _looks_transient(cls, failure_blob: str) -> bool:
        """True when the failure blob looks like an infra flake we should retry
        rather than patch: it carries a transient-network/runner marker AND has
        NO structured (assertion/import/JUnit/eval) failure to act on. Biased
        toward False — when in doubt, patch (a wasted rerun is cheaper than a
        skipped real fix only at the margin, but a false 'flaky' verdict that
        delays a real fix is worse, so require an explicit transient signal)."""
        if not failure_blob:
            return False
        low = failure_blob.lower()
        if any(m.lower() in low for m in cls._STRUCTURED_FAILURE_MARKERS):
            return False
        return any(m in low for m in cls._TRANSIENT_MARKERS)

    @staticmethod
    def _hash_files(files: List[Any]) -> str:
        """Deterministic hash of (path, content) tuples for loop detection."""
        h = hashlib.sha256()
        for cf in sorted(files, key=lambda f: f.path):
            h.update(cf.path.encode())
            h.update(b"\0")
            h.update((cf.new_content or "").encode())
            h.update(b"\0")
        return h.hexdigest()

    @classmethod
    def _touch(cls, entry: WatchEntry, msg: str, level: str = "info") -> None:
        entry.last_event_at = time.monotonic()
        logger.info(
            "CIWatcher[%s/%s#%s]: %s",
            entry.owner, entry.repo, entry.pr_number, msg,
        )
        # Append to the durable log (UI streams this) and re-persist the
        # entry so the status/attempts on disk track the live entry.
        try:
            ir.append_log(entry.owner, entry.repo, entry.pr_number, level, msg)
            cls._persist(entry)
        except Exception as e:
            logger.debug("_touch persistence failed: %s", e)

    @staticmethod
    async def _escalate(entry: WatchEntry, reason: str) -> None:
        """Post a PR comment when watcher gives up. Best-effort — don't raise."""
        body = (
            f"⚠️ **ShipMate AI auto-fix exhausted.**\n\n"
            f"**Reason:** {reason}\n"
            f"**Attempts:** {entry.attempts} / {MAX_ATTEMPTS}\n"
            f"**Originating finding:** `{entry.finding.kind}` / `{entry.finding.id}` — {entry.finding.title}\n\n"
            f"Manual review recommended. The branch has each attempt as a separate commit so you can `git revert` if needed."
        )
        try:
            await GitHubActionsService.post_pr_comment(
                entry.access_token, entry.owner, entry.repo, entry.pr_number, body,
            )
        except Exception as e:
            logger.warning("CIWatcher: escalation comment failed: %s", e)
