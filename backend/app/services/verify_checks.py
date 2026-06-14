"""
Concrete cheap checks for the inner verify-loop (Phase 4).

These adapt ShipMate's existing CHEAP validators into the verify_loop.Check
shape `(coder_out) -> CheckResult`. They compose the prior phases:

  • lint_check     — ast_lint hallucinated/stale imports + the orchestrator's
                     test-theater / injection regexes (the same _lint_coder_output
                     the actuate path already trusts). Pure, no IO. A syntax
                     error is FATAL (re-prompting in the cheap tier won't fix an
                     unparseable file reliably, and it can't even be smoke-tested).
  • scope_check    — scope_guard.check_patch (whole-file rewrites that drop
                     pre-existing defs / protected config lines). Pure text.
  • smoke_check    — applies the patch in a THROWAWAY sandbox and runs the
                     import smoke (entry point must still import). Sized by the
                     finding's gate tier (Phase 2 gate_for): skipped for a
                     lint-tier finding (docs — nothing to import), run otherwise.
                     Uses snapshot/restore so the real tree is untouched.

verify_loop only knows the Check protocol; keeping these here means the loop
module stays free of orchestrator / sandbox imports and trivially unit-testable.
Everything is fail-open: a checker that errors yields no issues (the loop treats
that check as passed), so a broken validator can never block a patch.
"""
from __future__ import annotations

import logging
from typing import Dict, List

from app.services.verify_loop import Check, CheckResult

logger = logging.getLogger("shipmate.verify_checks")


def _serialize(coder_out: object) -> List[Dict[str, str]]:
    """coder_out.files (duck-typed) → the [{path,new_content,rationale}] shape
    the scope guard / sandbox apply expect."""
    out: List[Dict[str, str]] = []
    for cf in getattr(coder_out, "files", []) or []:
        out.append({
            "path": getattr(cf, "path", ""),
            "new_content": getattr(cf, "new_content", ""),
            "rationale": getattr(cf, "rationale", ""),
        })
    return out


def make_lint_check(target_files: Dict[str, str], file_tree: List[str]) -> Check:
    """Build the lint check over the orchestrator's _lint_coder_output (AST
    import fidelity + stale-named imports + test-theater + injection sinks).
    A Python syntax error is reported FATAL so the loop stops rather than
    burning iterations on an unparseable file."""
    def _check(coder_out: object) -> CheckResult:
        try:
            from app.services.coder_orchestrator import _lint_coder_output
            issues = _lint_coder_output(coder_out, target_files, file_tree)
        except Exception as e:  # pragma: no cover - fail-open
            logger.debug("lint_check failed (%s) — treating as pass", e)
            return CheckResult(name="lint")
        fatal = any("syntax error" in i.lower() for i in issues)
        return CheckResult(name="lint", issues=list(issues), fatal=fatal)
    return _check


def make_scope_check(target_files: Dict[str, str]) -> Check:
    """Build the scope-discipline check over scope_guard.check_patch (catches a
    whole-file rewrite that silently drops pre-existing top-level defs or
    deletes protected config lines)."""
    def _check(coder_out: object) -> CheckResult:
        try:
            from app.services import scope_guard as sg
            serialized = _serialize(coder_out)
            summary = getattr(coder_out, "summary", "") or ""
            issues = sg.check_patch(serialized, target_files, summary)
        except Exception as e:  # pragma: no cover - fail-open
            logger.debug("scope_check failed (%s) — treating as pass", e)
            return CheckResult(name="scope")
        return CheckResult(name="scope", issues=list(issues))
    return _check


def make_smoke_check(finding_kind: str, finding_category: str = "") -> Check:
    """Build the sandbox import-smoke check. Applies the patch to a snapshot of
    the local tree, runs `smoke_imports` (entry point must still import), and
    ALWAYS restores — so the real tree is untouched. Sized by the finding's
    gate tier: a lint-tier finding (docs/prose) has nothing to import, so the
    check is a no-op pass. Only meaningful on ShipMate's own checkout (the
    smoke target is `app.main`); for an external repo there's no local tree, so
    it fails open to pass.

    This is the loop's only EXECUTION step, and it's deliberately the cheap one
    (a ~2s import, not the full pytest suite — that stays outside the loop)."""
    def _check(coder_out: object) -> CheckResult:
        try:
            from app.services import sandbox, validation_gate as vg
            tier = sandbox.gate_for(finding_kind, finding_category)
            if not tier.run_smoke:
                # docs/lint tier — nothing to import-test.
                return CheckResult(name="smoke")

            serialized = _serialize(coder_out)
            if not serialized:
                return CheckResult(name="smoke")

            # PREFER the worktree smoke: race-free + crash-safe, never mutates
            # the real tree — so it's safe to run BEFORE path claims, while a
            # concurrent actuate may be touching the tree. Falls back to the
            # snapshot smoke only when no worktree can be created.
            # ref defaults to HEAD — the SAME ref the Phase-2 pytest gate
            # (worktree_gate) validates against, so the smoke and the gate agree
            # on what code is being tested (and the dirty-tree fallback keeps
            # HEAD == working tree for the self-repo dogfood case).
            result = sandbox.worktree_smoke(serialized)
            if result is None:
                paths = [f["path"] for f in serialized]
                snap = vg.snapshot_files(paths)
                try:
                    vg.write_files_to_tree(serialized)
                    result = vg.smoke_imports()
                finally:
                    vg.restore_snapshot(snap)
            ok, err = result

            if not ok:
                return CheckResult(
                    name="smoke",
                    issues=[
                        "The patch breaks the entry-point import "
                        f"(`import app.main` failed): {err[:300]}. Fix the import "
                        "or definition so the module loads."
                    ],
                )
            return CheckResult(name="smoke")
        except Exception as e:  # pragma: no cover - fail-open
            logger.debug("smoke_check failed (%s) — treating as pass", e)
            return CheckResult(name="smoke")
    return _check


def default_checks(
    finding_kind: str,
    finding_category: str,
    target_files: Dict[str, str],
    file_tree: List[str],
    *,
    include_smoke: bool = True,
) -> List[Check]:
    """The standard cheap-check set for an actuate: lint + scope (+ sandbox
    smoke when include_smoke). Order matters only for issue readability; the
    loop aggregates all of them each iteration.

    `include_smoke` lets the caller drop the execution step for an external
    repo (no local tree to import against) while keeping the pure checks."""
    checks: List[Check] = [
        make_lint_check(target_files, file_tree),
        make_scope_check(target_files),
    ]
    if include_smoke:
        checks.append(make_smoke_check(finding_kind, finding_category))
    return checks
