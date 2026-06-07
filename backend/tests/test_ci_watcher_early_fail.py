"""Regression test for the CIWatcher early-failure short-circuit.

Bug: _await_ci waited until status.all_completed before returning, so a single
job wedged in GitHub's infra (queued / stuck in_progress for tens of minutes)
stalled the watcher until the 45-min wall clock — never acting on a failure it
could already see. PR #27 hit this exactly: frontend-lint = failure, backend-
pytest = stuck pending, watcher idle at 0 attempts.

Fix: once any check has FAILED and we've polled at least twice (settle), return
immediately so _supervise can drive a fix — the pending siblings don't change
the red verdict, and the fix commit re-triggers the whole run anyway.
"""
import asyncio

import pytest

from app.services.ci_watcher import CIWatcher, WatchEntry
from app.services.github_actions_service import CheckRunStatus
from app.schemas.api_schemas import FindingPayload


def _entry() -> WatchEntry:
    return WatchEntry(
        owner="o", repo="r", pr_number=27, branch="b", base_branch="main",
        access_token="tok",
        finding=FindingPayload(kind="milestone", id="M", title="x", description="d"),
        repo_lens=None,
    )


def _run_await_ci(monkeypatch, poll_statuses):
    """Drive _await_ci with a scripted sequence of CheckRunStatus per poll.
    Returns (result_status, num_polls_consumed)."""
    seq = iter(poll_statuses)
    state = {"polls": 0}

    async def fake_head_sha(token, owner, repo, branch):
        return "deadbeef"

    async def fake_list(token, owner, repo, ref):
        state["polls"] += 1
        try:
            return next(seq)
        except StopIteration:
            # If asked beyond the script, keep returning the last one.
            return poll_statuses[-1]

    async def fake_sleep(_):
        return None

    monkeypatch.setattr(
        "app.services.github_actions_service.GitHubActionsService.get_branch_head_sha",
        fake_head_sha,
    )
    monkeypatch.setattr(
        "app.services.github_actions_service.GitHubActionsService.list_check_runs",
        fake_list,
    )
    monkeypatch.setattr("app.services.ci_watcher.asyncio.sleep", fake_sleep)
    # _touch persists to sqlite + logs; stub it so the test stays offline.
    monkeypatch.setattr(CIWatcher, "_touch", classmethod(lambda cls, e, m: None))

    result = asyncio.run(CIWatcher._await_ci(_entry()))
    return result, state["polls"]


_FAILED_WITH_PENDING = CheckRunStatus([
    {"name": "frontend lint", "status": "completed", "conclusion": "failure", "id": 1},
    {"name": "backend pytest", "status": "in_progress", "conclusion": None, "id": 2},
])
_ALL_PENDING = CheckRunStatus([
    {"name": "frontend lint", "status": "in_progress", "conclusion": None, "id": 1},
    {"name": "backend pytest", "status": "in_progress", "conclusion": None, "id": 2},
])
_ALL_PASS = CheckRunStatus([
    {"name": "frontend lint", "status": "completed", "conclusion": "success", "id": 1},
    {"name": "backend pytest", "status": "completed", "conclusion": "success", "id": 2},
])


class TestAwaitCiEarlyFailure:
    def test_returns_on_failure_with_pending_siblings(self, monkeypatch):
        """A failed check + a stuck-pending sibling → return after the settle
        poll instead of waiting for the sibling to complete."""
        # poll 1: failed+pending (settle not met yet, polls<2 -> keep going)
        # poll 2: still failed+pending -> short-circuit
        result, polls = _run_await_ci(
            monkeypatch, [_FAILED_WITH_PENDING, _FAILED_WITH_PENDING, _FAILED_WITH_PENDING],
        )
        assert result is not None
        assert result.any_failed
        assert not result.all_completed   # proves we did NOT wait for completion
        assert polls == 2                 # acted on the 2nd poll (settle), not later

    def test_does_not_fire_on_first_poll_blip(self, monkeypatch):
        """Settle guard: a failure visible on the very first poll must wait one
        more poll (avoid acting on a transient first-poll state)."""
        # poll1 failed+pending, poll2 everything passed -> should return all_pass
        result, polls = _run_await_ci(
            monkeypatch, [_FAILED_WITH_PENDING, _ALL_PASS],
        )
        # On poll 2 all_completed is True → returns the passing status.
        assert result.all_passed
        assert polls == 2

    def test_waits_while_all_pending_no_failure(self, monkeypatch):
        """No failure yet, all pending → keep polling (until something resolves)."""
        result, polls = _run_await_ci(
            monkeypatch, [_ALL_PENDING, _ALL_PENDING, _ALL_PASS],
        )
        assert result.all_passed
        assert polls == 3   # waited through the pending polls

    def test_all_pass_returns_normally(self, monkeypatch):
        result, polls = _run_await_ci(monkeypatch, [_ALL_PASS])
        assert result.all_passed
        assert polls == 1
