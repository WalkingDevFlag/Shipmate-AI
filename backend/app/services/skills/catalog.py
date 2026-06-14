"""
The per-shape Coder skills. Each fragment carries ONLY the rules specific to
that finding shape — the universal rules live in base._BASE_RULES.

Provenance (which monolith rules moved where):
  • write-test     ← #2 no-test-theater, #3 existing-coverage, #9 placement,
                      and the "Reminders for tests specifically" block.
  • fix-security   ← #7 guardrail honesty (observably remove the problem).
  • add-feature    ← #5 dependency honesty + #10 new-deps-are-special.
  • fix-ci         ← CI/CD-specific guidance (workflow scope + the OAuth
                      `workflow` scope gotcha).
  • refactor       ← behaviour-preservation emphasis (uses base scope rules,
                      plus an explicit "no behaviour change" reminder).
  • fix-bug        ← reproduce-then-fix + regression-test guidance.
"""
from __future__ import annotations

from .base import Skill, TEST_QUALITY_RULES


WRITE_TEST = Skill(
    name="write-test",
    triggers="finding.kind == 'test', or category == 'testing'",
    gate_kind="test",
    fragment=(
        "## Skill: write-test — you are adding or extending tests\n\n"

        "A. EXISTING COVERAGE CHECK. Before adding tests, scan `target_files` "
        "for tests already covering the same function/endpoint. If coverage "
        "exists, EXTEND that file (add cases, parametrize) rather than "
        "creating a new file. Output a `skipped` entry naming the existing "
        "file. Goal: one test file per unit-under-test.\n\n"

        "B. The patch IS tests, so the test-quality rules below are the core of "
        "your job, not an afterthought.\n\n"

        + TEST_QUALITY_RULES
    ),
)


FIX_SECURITY = Skill(
    name="fix-security",
    triggers="finding.kind == 'guardrail', or security/hygiene categories "
             "(secrets, auth, cors, injection, config)",
    gate_kind="full",
    fragment=(
        "## Skill: fix-security — you are remediating a security/hygiene finding\n\n"

        "A. GUARDRAIL HONESTY. The patch must OBSERVABLY remove the problem. "
        "If the finding is 'committed .pyc files', do not output a `.gitkeep` "
        "inside `__pycache__/` — that preserves the directory you claim to "
        "remove. If history rewrite is needed, state so in `summary` and skip "
        "the file. Never produce a diff whose visible effect contradicts the "
        "commit message you'd write.\n\n"

        "B. PROVE IT. Prefer a change whose effect is checkable: a removed "
        "hardcoded secret, a narrowed CORS list, an added auth guard on the "
        "real route. If you can't make the fix observably remove the issue, "
        "`skipped` the file and explain why in `summary` rather than shipping "
        "a cosmetic change that leaves the problem in place.\n\n"

        "(Base rule #6 already forbids INTRODUCING eval/exec/shell sinks — that "
        "applies here too; don't add one while fixing something else.)"
    ),
)


ADD_FEATURE = Skill(
    name="add-feature",
    triggers="finding.kind in {'milestone','blocker','next_action'} with a "
             "feature/infra category (or default for those kinds)",
    gate_kind="full",
    fragment=(
        "## Skill: add-feature — you are building a feature / milestone\n\n"

        "A. NEW DEPS ARE A SPECIAL OPERATION. (Base rule #5 already requires any "
        "import to be manifest-backed.) Beyond that: if your patch genuinely "
        "needs a BRAND-NEW package, return `files` containing ONLY the manifest "
        "update plus a one-paragraph justification in `summary`; do not also "
        "write code that uses the dependency in the same patch — it comes in a "
        "follow-up brief.\n\n"

        "B. END-TO-END OR NOT AT ALL. A feature that adds a service but no "
        "caller, or a route but no registration, is rejected by the wire-it-up "
        "rule. If the full feature is too large for one honest patch, ship the "
        "smallest END-TO-END slice (one working path through service→route) "
        "and `skipped` the rest with a note.\n\n"

        "C. If your feature slice INCLUDES a test (the preamble anticipates "
        "'service + route + tests'), the test-quality rules below are "
        "mandatory for it.\n\n"

        + TEST_QUALITY_RULES
    ),
)


FIX_CI = Skill(
    name="fix-ci",
    triggers="category == 'ci_cd' (CI/CD workflow findings)",
    gate_kind="lint",
    fragment=(
        "## Skill: fix-ci — you are editing CI/CD workflow config\n\n"

        "A. WORKFLOW SCOPE. Edit only the workflow file(s) the finding names. "
        "A CI change must not also rewrite application code — that's scope "
        "drift. Keep YAML valid and the job graph intact (don't drop unrelated "
        "jobs/steps).\n\n"

        "B. KNOWN GOTCHA. Writing under `.github/workflows/` requires the "
        "GitHub OAuth token to carry the `workflow` scope; if that's missing "
        "the commit fails with a 404. You can't fix that here — just make the "
        "YAML change correctly; the orchestrator surfaces the scope error to "
        "the user.\n\n"

        "C. NO SECRETS IN YAML. Never inline a token/secret value — reference "
        "`${{ secrets.NAME }}`. Don't add a step that echoes secrets to logs."
    ),
)


REFACTOR = Skill(
    name="refactor",
    triggers="category == 'refactor', or a finding whose task is "
             "restructuring without behaviour change",
    gate_kind="full",
    fragment=(
        "## Skill: refactor — you are restructuring without changing behaviour\n\n"

        "A. BEHAVIOUR IS FROZEN. The observable behaviour (return values, "
        "side-effects, public signatures, route contracts) MUST be identical "
        "before and after. If a 'refactor' would change behaviour, it's not a "
        "refactor — `skipped` it and say so.\n\n"

        "B. NO MASS DROPS. Preserve every top-level definition that callers "
        "rely on. The scope guard rejects a rewrite that silently drops "
        "pre-existing defs — move code, don't delete it.\n\n"

        "C. SMALL STEPS. A refactor PR should be mechanically reviewable: "
        "renames, extractions, de-duplication. Don't fold a behaviour change "
        "or a new dependency into it."
    ),
)


FIX_BUG = Skill(
    name="fix-bug",
    triggers="category == 'bug', or a defect-fix task not matching the above",
    gate_kind="full",
    fragment=(
        "## Skill: fix-bug — you are fixing a defect\n\n"

        "A. ROOT CAUSE, NOT SYMPTOM. Identify why the bug happens from the "
        "real code in `target_files` and fix the cause. Don't paper over it "
        "with a broad try/except or a special-case that hides the failure.\n\n"

        "B. MINIMAL DIFF. Change only what the fix requires. A one-line cause "
        "gets a one-line fix — don't rewrite the surrounding function.\n\n"

        "C. GUARD AGAINST REGRESSION. If a target test file is present and the "
        "fix is unit-testable, add a focused case that would FAIL on the old "
        "behaviour and PASS on the new — following the test-quality rules "
        "below. If no test file is in scope, note in `summary` what test "
        "should follow.\n\n"

        + TEST_QUALITY_RULES
    ),
)


# Every concrete (non-base) skill, in dispatch-priority documentation order.
SKILLS = [
    WRITE_TEST,
    FIX_SECURITY,
    FIX_CI,
    ADD_FEATURE,
    REFACTOR,
    FIX_BUG,
]
