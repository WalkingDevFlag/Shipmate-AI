"""
Coder agent — generates full file contents for a focused patch.

Different shape from the 4 diagnostic agents:
  • RepoLens / PlanForge / GuardRail / TestPilot take a `context: Dict` and
    return Pydantic *report* outputs (heuristic + LLM-enhanced prose).
  • Coder takes a focused brief — one finding + 1–5 target files — and
    returns full new file contents that will be committed verbatim.

Why full files (not diffs):
  LLMs reliably emit valid full files; unified diffs frequently fail to apply
  (line drift, whitespace). The GitHub Contents API takes full content
  anyway — full-file output is the natural shape.

Failure semantics:
  Bedrock unreachable / schema-validation / token cap → caller (orchestrator)
  catches and returns a 500 with the actual error. We do NOT silently fall
  back to a stub here — the user clicked "Apply Fix" expecting real work.
"""

from __future__ import annotations

import logging
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, Field

from app.services.bedrock_provider import BedrockProvider
from app.services.llm_provider import get_provider

logger = logging.getLogger("shipmate.coder_agent")

_SYSTEM_PROMPT = (
    "You are a senior staff engineer producing a focused, surgical patch on "
    "behalf of an AI release-readiness platform called ShipMate.\n\n"
    "## Inputs\n"
    "You will receive: (a) a single task, (b) compact repo context, "
    "(c) the current contents of 1-5 target files. For each file you choose "
    "to change, output the COMPLETE new file content — no diffs, no '...', "
    "no placeholders, no truncation. Match existing style: indentation, "
    "import order, naming. The patch should be focused — touch only the "
    "files genuinely required for THIS finding. There is no enforced "
    "file cap, but a smaller, surgical patch is always preferred over a "
    "broad rewrite. If a finding genuinely needs many files (e.g. an "
    "end-to-end feature touching service + route + tests + types), do "
    "all of it correctly; if you need 1 file, return 1.\n\n"

    "## Hard rules — violations are rejected\n\n"

    "1. GROUND TRUTH — IMPORT FIDELITY IS THE #1 RULE. When you rewrite "
    "an EXISTING file (one whose current content was shown in "
    "target_files), the new file's import block MUST be derived from the "
    "ORIGINAL import block in target_files. Specifically:\n"
    "   • You may KEEP any import that appears in the original.\n"
    "   • You may REMOVE imports the patch no longer uses.\n"
    "   • You may ADD an import only if it is (a) Python stdlib (re, os, "
    "secrets, hashlib, hmac, json, asyncio, typing, dataclasses, "
    "datetime, logging, pathlib), or (b) a package already imported "
    "elsewhere in the same target file's original, or (c) a project "
    "module whose path is present in `target_files` or visible in the "
    "repo `entry_points`.\n"
    "   • You MAY NOT invent imports based on what a typical project of "
    "this kind would have. Do NOT add `from app.db.database import "
    "init_db`, `from sqlalchemy …`, `from app.services.X` unless that "
    "exact symbol appears in the ORIGINAL file's imports OR is supplied "
    "in target_files. The phrase 'the codebase might have it' is FORBIDDEN.\n"
    "   • You MAY NOT reorganize the file by stripping unrelated routers/"
    "middleware/imports from the original. If the original had "
    "`from app.api.routes.actuate import router as actuate_router` and "
    "registered it via `app.include_router(actuate_router, ...)`, your "
    "rewrite MUST preserve those lines verbatim unless removing them is "
    "the literal task. Touching unrelated routers is rejected as scope "
    "drift.\n"
    "   If you cannot honestly satisfy this rule for a file, list it "
    "under `skipped` with a one-line reason in `summary`. A 1-file honest "
    "patch beats a 3-file rewrite that invents imports.\n\n"

    "2. NO TEST THEATER. If you write tests:\n"
    "   - exercise the REAL application object (import the actual app, "
    "     orchestrator, route handler — do NOT build a hand-rolled FastAPI "
    "     app inside the test file).\n"
    "   - each test must have a SINGLE concrete expected outcome that "
    "     would FAIL if the production code regressed.\n"
    "   - FORBIDDEN: asserting on a mock you just configured; asserting "
    "     on object fields you constructed in the same test; asserting "
    "     `status_code in [200, 401, 404, 405]` (any assertion accepting "
    "     a 4xx as success); asserting `not_x or some_truthy_thing` "
    "     (tautologies); testing library internals (PyJWT, TypeScript's "
    "     own type checker, etc.) instead of YOUR code.\n\n"

    "3. EXISTING COVERAGE CHECK. Before adding tests, scan `target_files` "
    "for tests already covering the same function/endpoint. If coverage "
    "exists, EXTEND that file (add cases, parametrize) rather than "
    "creating a new file. Output a `skipped` entry naming the existing "
    "file. Goal: one test file per unit-under-test.\n\n"

    "4. SCOPE DISCIPLINE. The patch addresses the SINGLE finding identified "
    "by `finding_kind/finding_id`. Do NOT delete unrelated code, do NOT "
    "change unrelated API contracts, do NOT bundle cosmetic edits "
    "(whitespace, unicode dashes, comment rewrites) into a security or "
    "feature PR. If you find yourself rewriting more than was asked, "
    "stop and put the extras under `skipped` with a one-line note.\n\n"

    "5. DEPENDENCY HONESTY. If your patch imports a package, that package "
    "MUST already be in `requirements.txt` / `package.json` (check the "
    "manifest in `target_files` if present), OR you MUST include the "
    "manifest in `files` with the dependency added. Never introduce "
    "SQLAlchemy, Jest, Vitest, or any runtime/test framework without "
    "wiring it into the manifest AND its config (jest.config, "
    "vitest.config, pyproject) in the same patch.\n\n"

    "6. WIRE IT UP. Infrastructure without a caller is FORBIDDEN. If you "
    "create a service module, persistence helper, or middleware, you "
    "MUST also include the file that calls it (router, startup hook, "
    "FastAPI dependency). A patch that defines `persist_report()` but "
    "never calls it from `/api/analyze` is rejected — surface this as "
    "`skipped` with reasoning instead.\n\n"

    "7. GUARDRAIL HONESTY. For security/hygiene findings, the patch must "
    "OBSERVABLY remove the problem. If the finding is 'committed .pyc "
    "files', do not output a `.gitkeep` inside `__pycache__/` — that "
    "preserves the directory you claim to remove. If history rewrite is "
    "needed, state so in `summary` and skip the file. Never produce a "
    "diff whose visible effect contradicts the commit message you'd write.\n\n"

    "8. CONTRACT STABILITY. Do NOT change the signature of public "
    "functions, route paths, or request/response schemas unless the "
    "task explicitly says so. If you must, every caller in target_files "
    "must be updated in the same patch.\n\n"

    "9. TEST PLACEMENT. Backend tests go in `backend/tests/`. Frontend "
    "tests go in `frontend/src/__tests__/` or alongside the component "
    "(`*.test.tsx`). Never place tests at repo-root `/tests/`.\n\n"

    "10. NEW DEPS ARE A SPECIAL OPERATION. If your patch genuinely needs "
    "a new package, return `files` containing ONLY the manifest update "
    "plus a one-paragraph justification in `summary`; do not also write "
    "code that uses the dependency in the same patch — it comes in a "
    "follow-up brief.\n\n"

    "## Self-check before returning — MANDATORY\n"
    "The LAST line of your `summary` MUST literally be:\n"
    "  VERIFY: imports-grounded, no-theater, scope-ok, deps-ok\n"
    "(or replace each token with FAIL:<reason> for any rule you violated, "
    "in which case fix the patch before returning).\n"
    "If your summary doesn't end with a `VERIFY:` line, the entire patch "
    "is rejected by post-processing — no PR will be opened. The line is "
    "non-optional and not negotiable. Write it.\n\n"

    "## NO PHANTOM PATCHES — the worst violation\n"
    "A `VERIFY:` line asserts you actually produced the patch. If you return "
    "ZERO file edits (every target under `skipped`, `files` empty), you MUST "
    "NOT write a clean `VERIFY:` line — that is a phantom patch that claims a "
    "fix you never made, and it is the single worst thing you can do. When you "
    "produce no file edits, your summary MUST instead begin with literally "
    "`DECLINED: ` followed by a one-line reason (e.g. "
    "`DECLINED: the change spans 4 large files and needs decomposition`), and "
    "the VERIFY line MUST read `VERIFY: declined — no patch produced`. Choose "
    "exactly one: a real patch with a real VERIFY line, or an honest DECLINED "
    "with no clean VERIFY. Never an empty patch dressed as a success.\n\n"

    "## Reminders for tests specifically\n"
    "  • NEVER write `assert response.status_code in [200, 401, 404]` or "
    "any tuple/list of mixed-success-and-error codes. Pick ONE expected "
    "code and assert exactly that.\n"
    "  • If you can't predict the exact status code, you don't understand "
    "the code path well enough to test it — `skipped` it instead.\n"
    "  • `assert True` is forbidden. `pytest.skip()` and "
    "`@pytest.mark.skip` are forbidden.\n"
    "  • Tests of a function MUST import that function from its real "
    "location (e.g. `from app.main import sanitize_input_middleware`); "
    "constructing a separate FastAPI app inside the test = rejected.\n\n"

    "If a file in `target_files` doesn't need to change, list it under "
    "`skipped`. Provide a one-sentence rationale per file. Keep the "
    "summary under 5 sentences plus the mandatory VERIFY line."
)

# Diff-mode output-format override — appended to whichever system prompt is in
# use (the legacy monolith OR a skill-composed prompt). Factored out so
# system_prompt_for() can append it to a composed prompt too.
_DIFF_OUTPUT_OVERRIDE = (
    "\n\n# OUTPUT FORMAT OVERRIDE — UNIFIED DIFF MODE\n"
    "Instead of full file contents, emit a UNIFIED DIFF per changed file in "
    "the `unified_diff` field:\n"
    "  • Use `@@ -oldStart,oldCount +newStart,newCount @@` hunk headers.\n"
    "  • Prefix unchanged context lines with a single space, removed lines "
    "with '-', added lines with '+'.\n"
    "  • Include AT LEAST 3 lines of real, verbatim context above and below "
    "each change so the hunk can be located even if line numbers drifted.\n"
    "  • Context/removed lines MUST exactly match the current file content "
    "(byte-for-byte, including indentation) — a mismatch makes the diff fail "
    "to apply and the whole patch is discarded.\n"
    "  • Do NOT include ---/+++ file header lines; the `path` field names "
    "the file.\n"
    "  • Only use diff mode for EDITS to existing files. For a brand-new "
    "file, you cannot diff — list it in `skipped` and note that it needs "
    "full-file mode.\n"
    "All the scope/test/anti-theater rules above still apply to the diff."
)

# Legacy monolithic diff prompt — retained as the fail-open fallback target.
_SYSTEM_PROMPT_DIFF = _SYSTEM_PROMPT + _DIFF_OUTPUT_OVERRIDE


class CoderFile(BaseModel):
    path: str = Field(..., description="Relative repo path, e.g. 'app/main.py' or '.github/workflows/ci.yml'.")
    new_content: str = Field(..., description="Complete new file content, no diff syntax, no truncation.")
    rationale: str = Field(..., description="One sentence explaining why this file changed.")


class CoderOutput(BaseModel):
    files: List[CoderFile] = Field(default_factory=list)
    skipped: List[str] = Field(default_factory=list, description="Paths from target_files that did not need changes.")
    summary: str = Field(..., description="1-2 sentence summary of what the patch does and why.")


class CoderFileDiff(BaseModel):
    """Diff-mode counterpart of CoderFile — a unified diff, not full content."""
    path: str = Field(..., description="Relative repo path being patched (must be an existing file).")
    unified_diff: str = Field(
        ...,
        description=(
            "A unified diff for this file: @@ -l,s +l,s @@ hunk headers with "
            "' ' context, '-' removed, '+' added lines. No ---/+++ needed. "
            "Include 3 lines of context around each change."
        ),
    )
    rationale: str = Field(..., description="One sentence explaining why this file changed.")


class CoderOutputDiff(BaseModel):
    files: List[CoderFileDiff] = Field(default_factory=list)
    skipped: List[str] = Field(default_factory=list, description="Paths that did not need changes.")
    summary: str = Field(..., description="1-2 sentence summary of what the patch does and why.")


class CoderBrief(BaseModel):
    """Everything Coder needs to produce a patch — built by the orchestrator."""
    task: str
    repo_full_name: str
    primary_language: str = "Unknown"
    tech_stack: List[str] = Field(default_factory=list)
    entry_points: List[str] = Field(default_factory=list)
    target_files: Dict[str, str] = Field(default_factory=dict, description="path -> current content (empty for new files)")
    repo_map: str = Field(
        default="",
        description=(
            "Real module/symbol map of the package(s) the target files live in "
            "(built by repo_map.build_repo_map). Anti-hallucination: the model "
            "imports only from modules/symbols listed here. Empty ⇒ omit the section."
        ),
    )
    finding_kind: str
    finding_id: str
    finding_severity: Optional[str] = None
    finding_category: str = Field(
        default="",
        description=(
            "Finding category (e.g. secrets, ci_cd, testing, refactor). With "
            "finding_kind it selects the Coder SKILL (skills.skill_for) so the "
            "system prompt carries only the rules that finding shape needs."
        ),
    )


def _truncate_for_prompt(content: str, max_chars: int = 30_000) -> str:
    if len(content) <= max_chars:
        return content
    head = content[: max_chars - 200]
    return (
        head
        + f"\n\n# ... [truncated by ShipMate; original {len(content)} chars]\n"
    )


def _format_files_block(target_files: Dict[str, str]) -> str:
    parts: List[str] = []
    for path, content in target_files.items():
        body = _truncate_for_prompt(content) if content else "(new file — does not exist yet)"
        parts.append(f"### File: {path}\n```\n{body}\n```")
    return "\n\n".join(parts) if parts else "(no target files supplied)"


def _build_user_prompt(brief: CoderBrief) -> str:
    stack = ", ".join(brief.tech_stack) or "unknown"
    entries = ", ".join(brief.entry_points[:5]) or "unknown"
    # The repo map (when present) goes BEFORE the target files: the model reads
    # the real namespace first, so when it later writes import lines it has the
    # true module/symbol list in front of it instead of guessing a plausible-
    # sounding sibling. Empty map ⇒ omit the section entirely.
    repo_map_block = f"\n{brief.repo_map}\n" if brief.repo_map else ""
    return (
        f"# Task\n{brief.task}\n\n"
        f"# Repo\n"
        f"- Name: {brief.repo_full_name}\n"
        f"- Primary language: {brief.primary_language}\n"
        f"- Tech stack: {stack}\n"
        f"- Entry points: {entries}\n"
        f"- Originating finding: {brief.finding_kind}/{brief.finding_id}"
        f"{f' (severity {brief.finding_severity})' if brief.finding_severity else ''}\n"
        f"{repo_map_block}\n"
        f"# Target files\n{_format_files_block(brief.target_files)}\n\n"
        f"Produce the patch. Return ONLY the structured CoderOutput object."
    )


def system_prompt_for(brief: CoderBrief, diff_mode: bool = False) -> str:
    """Compose the Coder system prompt for THIS finding via the skill registry
    (Phase 3): base/universal rules + the one skill fragment matched by
    (finding_kind, finding_category). Shorter and sharper than the old
    monolith, which carried every kind's rules on every patch.

    Fail-open: if the skills package can't be imported or dispatch errors, fall
    back to the legacy monolithic _SYSTEM_PROMPT so the Coder always has a valid
    prompt. `diff_mode=True` appends the unified-diff output-format override."""
    try:
        from app.services import skills
        skill = skills.skill_for(brief.finding_kind, brief.finding_category or "")
        prompt = skills.compose_system_prompt(skill)
    except Exception as e:  # pragma: no cover - fail-open to the monolith
        logger.debug("skill composition failed (%s) — using legacy monolith prompt", e)
        prompt = _SYSTEM_PROMPT
    if diff_mode:
        prompt = prompt + _DIFF_OUTPUT_OVERRIDE
    return prompt


class CoderAgent:
    name = "coder"
    description = "Generates full file contents to remediate a single finding"

    def __init__(self, provider: Optional[BedrockProvider] = None) -> None:
        self._provider = provider

    def _get_provider(self) -> BedrockProvider:
        # Routed through the factory so SHIPMATE_LLM_PROVIDER=azure swaps in the
        # AzureOpenAIProvider (same invoke_structured contract). The type hint
        # stays BedrockProvider for back-compat; both providers are structurally
        # identical from the caller's side.
        if self._provider is None:
            self._provider = get_provider()
        return self._provider

    def run(
        self,
        brief: CoderBrief,
        deployment_hint: Literal["smart", "fast"] = "smart",
        mode: Literal["full", "diff"] = "full",
    ) -> CoderOutput:
        """
        Synchronous (the underlying Bedrock SDK is sync; we call the
        `_sync` variant to stay safe inside FastAPI's running event loop —
        the route awaits us via `asyncio.to_thread`).

        `mode="diff"` asks the model for unified diffs (token-saving on large
        files); the diffs are applied here and the method STILL returns a
        normal CoderOutput (full new_content per file), so every downstream
        consumer (lint, scope guard, pytest gate, GitHub commit) is unchanged.
        On any diff-apply failure it transparently falls back to full mode.
        """
        if mode == "diff":
            return self._run_diff_with_fallback(brief, deployment_hint)

        provider = self._get_provider()
        user_prompt = _build_user_prompt(brief)
        system_prompt = system_prompt_for(brief)

        logger.info(
            "Coder.run kind=%s id=%s cat=%s files=%d hint=%s",
            brief.finding_kind, brief.finding_id, brief.finding_category or "-",
            len(brief.target_files), deployment_hint,
        )

        result = provider.invoke_structured_sync(
            system_prompt=system_prompt,
            user_prompt=user_prompt,
            schema_class=CoderOutput,
            deployment_hint=deployment_hint,
        )
        return self._cap_runaway(result)

    def _run_diff_with_fallback(
        self,
        brief: CoderBrief,
        deployment_hint: Literal["smart", "fast"] = "smart",
    ) -> CoderOutput:
        """Diff-mode path: ask for unified diffs, apply them against the
        brief's target_files, and return a full-content CoderOutput. Falls
        back to a full-file `run(mode='full')` if the model emits no diffs or
        any diff fails to apply / breaks .py syntax."""
        from app.services import diff_apply as da

        provider = self._get_provider()
        user_prompt = _build_user_prompt(brief)
        logger.info(
            "Coder.run(diff) kind=%s id=%s files=%d",
            brief.finding_kind, brief.finding_id, len(brief.target_files),
        )
        diff_out: CoderOutputDiff = provider.invoke_structured_sync(
            system_prompt=system_prompt_for(brief, diff_mode=True),
            user_prompt=user_prompt,
            schema_class=CoderOutputDiff,
            deployment_hint=deployment_hint,
        )

        if not diff_out.files:
            logger.info("Coder.run(diff): no diffs returned — falling back to full mode")
            return self.run(brief, deployment_hint, mode="full")

        files: List[CoderFile] = []
        for fd in diff_out.files:
            original = brief.target_files.get(fd.path, "")
            if not original.strip():
                logger.info(
                    "Coder.run(diff): %s is new/empty — diff can't apply, fallback to full",
                    fd.path,
                )
                return self.run(brief, deployment_hint, mode="full")
            patched, err = da.apply_and_validate(original, fd.unified_diff, fd.path)
            if patched is None:
                logger.info(
                    "Coder.run(diff): apply failed for %s (%s) — fallback to full",
                    fd.path, err,
                )
                return self.run(brief, deployment_hint, mode="full")
            files.append(CoderFile(
                path=fd.path, new_content=patched, rationale=fd.rationale,
            ))

        return self._cap_runaway(CoderOutput(
            files=files, skipped=diff_out.skipped, summary=diff_out.summary,
        ))

    def run_with_lint_feedback(
        self,
        brief: CoderBrief,
        lint_issues: List[str],
        deployment_hint: Literal["smart", "fast"] = "smart",
    ) -> CoderOutput:
        """Re-run after post-Coder lint rejected the first patch, feeding the
        specific issues back so the model corrects them. Used by the
        orchestrator for one auto-retry before returning `lint_rejected`."""
        provider = self._get_provider()
        user_prompt = _build_user_prompt(brief)
        logger.info(
            "Coder.run_with_lint_feedback kind=%s id=%s issues=%d",
            brief.finding_kind, brief.finding_id, len(lint_issues),
        )
        result = provider.invoke_with_lint_feedback(
            system_prompt=system_prompt_for(brief),
            user_prompt=user_prompt,
            schema_class=CoderOutput,
            lint_issues=lint_issues,
            deployment_hint=deployment_hint,
        )
        return self._cap_runaway(result)

    @staticmethod
    def _cap_runaway(result: CoderOutput) -> CoderOutput:
        # Sanity-only soft cap. Anything above 25 files in a single patch
        # is almost certainly a runaway generation — log and trim. Below
        # that, trust the model: real refactors and feature work
        # legitimately need >5 files.
        _RUNAWAY_FILE_LIMIT = 25
        if len(result.files) > _RUNAWAY_FILE_LIMIT:
            logger.warning(
                "Coder returned %d files (over runaway limit %d); truncating",
                len(result.files), _RUNAWAY_FILE_LIMIT,
            )
            result.files = result.files[:_RUNAWAY_FILE_LIMIT]
        return result
