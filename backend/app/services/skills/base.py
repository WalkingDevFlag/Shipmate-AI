"""
The Skill dataclass + the BASE skill (universal rules) + prompt composition.

The universal rules below are lifted VERBATIM (in substance) from the rules of
the original monolithic Coder prompt that apply to EVERY finding shape:
  • #1 import fidelity / ground truth  — the #1 rule, always
  • #4 scope discipline                — always
  • #6 wire-it-up                       — always
  • #8 contract stability               — always
  • the inputs/output-format preamble + the mandatory VERIFY self-check.

The kind-specific rules (#2/#3 test-theater, #5/#10 dependencies, #7 guardrail
honesty, #9 test placement) move OUT of base and into the per-shape skills in
this package, so a given patch only carries the rules that apply to it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import List


@dataclass(frozen=True)
class Skill:
    """One Coder behaviour profile.

    name:         stable identifier (also the coder_lessons / golden key).
    triggers:     human note on what routes here (doc only; dispatch lives in
                  registry.skill_for).
    gate_kind:    the validation tier this work warrants — mirrors the
                  sandbox.gate_for vocabulary ('full' | 'test' | 'import-smoke'
                  | 'lint') so the gate tier is declared next to the skill.
    fragment:     the skill-specific block appended after the base rules.
    """
    name: str
    triggers: str
    gate_kind: str
    fragment: str = ""


# ── Shared preamble (inputs + output format) ─────────────────────────────────

_PREAMBLE = (
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
)

# ── Universal rules (apply to every finding shape) ───────────────────────────

_BASE_RULES = (
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
    "repo map / `entry_points`.\n"
    "   • You MAY NOT invent imports based on what a typical project of "
    "this kind would have. Do NOT add `from app.db.database import "
    "init_db`, `from sqlalchemy …`, `from app.services.X` unless that "
    "exact symbol appears in the ORIGINAL file's imports OR is supplied "
    "in target_files OR is listed in the repo map. The phrase 'the "
    "codebase might have it' is FORBIDDEN.\n"
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

    "2. SCOPE DISCIPLINE. The patch addresses the SINGLE finding identified "
    "by `finding_kind/finding_id`. Do NOT delete unrelated code, do NOT "
    "change unrelated API contracts, do NOT bundle cosmetic edits "
    "(whitespace, unicode dashes, comment rewrites) into a security or "
    "feature PR. If you find yourself rewriting more than was asked, "
    "stop and put the extras under `skipped` with a one-line note.\n\n"

    "3. WIRE IT UP. Infrastructure without a caller is FORBIDDEN. If you "
    "create a service module, persistence helper, or middleware, you "
    "MUST also include the file that calls it (router, startup hook, "
    "FastAPI dependency). A patch that defines `persist_report()` but "
    "never calls it from `/api/analyze` is rejected — surface this as "
    "`skipped` with reasoning instead.\n\n"

    "4. CONTRACT STABILITY. Do NOT change the signature of public "
    "functions, route paths, or request/response schemas unless the "
    "task explicitly says so. If you must, every caller in target_files "
    "must be updated in the same patch.\n\n"

    "5. DEPENDENCY HONESTY. ANY patch — test, fix, feature, refactor — that "
    "imports a package MUST ensure that package is already in "
    "`requirements.txt` / `package.json` (check the manifest in `target_files` "
    "if present), OR include the manifest in `files` with the dependency "
    "added. Never introduce SQLAlchemy, Jest, Vitest, pytest plugins, or any "
    "runtime/test framework without wiring it into the manifest AND its config "
    "(jest.config, vitest.config, pyproject) in the same patch. (Adding a NEW "
    "package is a special operation — see the add-feature skill.)\n\n"

    "6. NO NEW DYNAMIC-EXECUTION SINKS. No patch of any kind may INTRODUCE an "
    "eval / exec / __import__ / os.system / subprocess(..., shell=True) call "
    "that wasn't in the original file. It's the exact pattern the inbound "
    "sanitizer blocks, and post-lint rejects a patch that adds one. Use a safe "
    "alternative (ast.literal_eval, argument lists without a shell).\n\n"
)

# ── Mandatory self-check (always last) ───────────────────────────────────────

_VERIFY = (
    "## Self-check before returning — MANDATORY\n"
    "The LAST line of your `summary` MUST literally be:\n"
    "  VERIFY: imports-grounded, no-theater, scope-ok, deps-ok\n"
    "(or replace each token with FAIL:<reason> for any rule you violated, "
    "in which case fix the patch before returning).\n"
    "If your summary doesn't end with a `VERIFY:` line, the entire patch "
    "is rejected by post-processing — no PR will be opened. The line is "
    "non-optional and not negotiable. Write it.\n\n"
    "If a file in `target_files` doesn't need to change, list it under "
    "`skipped`. Provide a one-sentence rationale per file. Keep the "
    "summary under 5 sentences plus the mandatory VERIFY line."
)


# ── Shared test-quality rules ────────────────────────────────────────────────
# Any skill whose patch MAY add tests (write-test, but also add-feature and
# fix-bug, which the prompts explicitly invite to add a regression test) must
# carry these — else a feature/bug patch could ship test theater the monolith
# would have forbidden. Skills embed this constant rather than re-stating it.
TEST_QUALITY_RULES = (
    "### If this patch ADDS or EDITS tests — test-quality rules (no theater)\n"
    "   - exercise the REAL application object (import the actual app, "
    "orchestrator, route handler — do NOT build a hand-rolled FastAPI app "
    "inside the test file).\n"
    "   - each test must have a SINGLE concrete expected outcome that would "
    "FAIL if the production code regressed.\n"
    "   - FORBIDDEN: asserting on a mock you just configured; asserting on "
    "object fields you constructed in the same test; asserting "
    "`status_code in [200, 401, 404, 405]` (any assertion accepting a 4xx as "
    "success); tautologies like `assert not_x or something_truthy`; testing "
    "library internals instead of YOUR code; `assert True`; `pytest.skip()` / "
    "`@pytest.mark.skip`.\n"
    "   - pick ONE expected status code and assert exactly that; if you can't "
    "predict it you don't understand the path well enough — skip the test.\n"
    "   - PLACEMENT: backend tests in `backend/tests/`, frontend tests in "
    "`frontend/src/__tests__/` or alongside the component (`*.test.tsx`); "
    "never at repo-root `/tests/`.\n"
    "   - import the unit-under-test from its REAL location."
)


BASE_SKILL = Skill(
    name="base",
    triggers="universal — always composed in, under every other skill",
    gate_kind="full",
    fragment="",
)


def compose_system_prompt(skill: Skill) -> str:
    """Build the full system prompt for `skill`: shared preamble + universal
    base rules + the skill's own fragment + the mandatory VERIFY self-check.

    Always returns at least the base prompt — passing BASE_SKILL (or a skill
    with an empty fragment) yields the universal rules with no kind-specific
    section, which is the correct floor for an unknown finding shape."""
    parts: List[str] = [_PREAMBLE, _BASE_RULES]
    frag = (skill.fragment or "").strip()
    if frag:
        parts.append(frag + "\n\n")
    parts.append(_VERIFY)
    return "".join(parts)
