"""Coder skill registry (Phase 3).

The Coder's monolithic ~130-line prompt held 10 rules, most kind-specific noise
for any given finding. The registry decomposes it into a base skill (universal
rules) + one skill per finding shape, dispatched deterministically.

These tests pin:
  • dispatch picks the right skill per (kind, category), with documented
    precedence and a fail-safe base fallback for unknown shapes;
  • the composed prompt ALWAYS keeps the universal invariants (import fidelity,
    scope discipline) and the mandatory VERIFY self-check;
  • kind-specific rules are correctly ISOLATED (test-theater only in write-test,
    guardrail-honesty only in fix-security) — the whole point of the split;
  • every composed prompt is SHORTER than the old monolith for a single finding;
  • each skill's gate_kind matches sandbox.gate_for (the Phase-2 tiers derive
    from the skill, so they can't drift);
  • system_prompt_for fails open to the monolith and appends diff-mode override.
"""
import pytest

from app.services import skills
from app.services.skills import BASE_SKILL, compose_system_prompt, skill_for
from app.services import sandbox
from app.agents.coder_agent import (
    CoderBrief, system_prompt_for, _SYSTEM_PROMPT, _SYSTEM_PROMPT_DIFF,
)


# ── Dispatch ─────────────────────────────────────────────────────────────────

class TestDispatch:
    @pytest.mark.parametrize("kind,category,expected", [
        ("test", "", "write-test"),
        ("milestone", "testing", "write-test"),       # category wins for testing
        ("guardrail", "", "fix-security"),
        ("guardrail", "injection", "fix-security"),
        ("milestone", "secrets", "fix-security"),     # security category routes here
        ("milestone", "ci_cd", "fix-ci"),
        ("blocker", "refactor", "refactor"),
        ("next_action", "bug", "fix-bug"),
        ("milestone", "feature", "add-feature"),
        ("blocker", "", "add-feature"),               # feature kind default
        ("next_action", "", "add-feature"),
    ])
    def test_routes_to_expected_skill(self, kind, category, expected):
        assert skill_for(kind, category).name == expected

    def test_unknown_shape_falls_back_to_base(self):
        s = skill_for("totally-unknown", "weird-cat")
        assert s.name == "base"
        assert s is BASE_SKILL

    def test_test_kind_beats_security_category(self):
        # A 'test' kind is unambiguous and must not be diluted even if some
        # odd category is attached — testing precedence is first.
        assert skill_for("test", "injection").name == "write-test"

    def test_case_and_whitespace_insensitive(self):
        assert skill_for("  TEST  ", "  TESTING ").name == "write-test"
        assert skill_for("GuardRail", "SECRETS").name == "fix-security"

    @pytest.mark.parametrize("kind", ["milestone", "blocker", "next_action"])
    def test_security_category_routes_to_fix_security(self, kind):
        # Review finding (high): 'security' is a real PlanForge/opportunity
        # milestone category — it must route to fix-security, not add-feature.
        assert skill_for(kind, "security").name == "fix-security"

    def test_config_category_is_guardrail_only(self):
        # Review finding (high): 'config' is overloaded — GuardRail hygiene vs
        # structural config. Only a guardrail-kind config finding is security;
        # a milestone/next_action config edit is structural → its kind's skill.
        assert skill_for("guardrail", "config").name == "fix-security"
        assert skill_for("milestone", "config").name == "add-feature"
        assert skill_for("next_action", "config").name == "add-feature"


# ── Composition invariants ───────────────────────────────────────────────────

_ALL_CASES = [
    ("test", "testing"), ("guardrail", "injection"), ("guardrail", "secrets"),
    ("milestone", "ci_cd"), ("milestone", "feature"), ("blocker", "refactor"),
    ("next_action", "bug"), ("weird", "unknown"),
]


class TestComposition:
    @pytest.mark.parametrize("kind,category", _ALL_CASES)
    def test_universal_rules_always_present(self, kind, category):
        p = compose_system_prompt(skill_for(kind, category))
        assert "IMPORT FIDELITY" in p
        assert "SCOPE DISCIPLINE" in p
        assert "WIRE IT UP" in p
        assert "CONTRACT STABILITY" in p

    @pytest.mark.parametrize("kind,category", _ALL_CASES)
    def test_verify_self_check_always_last(self, kind, category):
        p = compose_system_prompt(skill_for(kind, category))
        assert "VERIFY: imports-grounded, no-theater, scope-ok, deps-ok" in p

    @pytest.mark.parametrize("kind,category", _ALL_CASES)
    def test_shorter_than_monolith(self, kind, category):
        p = compose_system_prompt(skill_for(kind, category))
        assert len(p) < len(_SYSTEM_PROMPT), (
            f"{kind}/{category} composed prompt should be shorter than the monolith"
        )

    def test_base_only_has_no_skill_section(self):
        # The fail-safe floor: universal rules with no kind-specific block.
        p = compose_system_prompt(BASE_SKILL)
        assert "## Skill:" not in p
        assert "IMPORT FIDELITY" in p and "VERIFY:" in p


# ── Rule isolation (the point of the split) ──────────────────────────────────

class TestRuleIsolation:
    def test_test_theater_only_in_test_writing_shapes(self):
        # The no-theater rules live in the shared TEST_QUALITY_RULES layer,
        # carried by write-test (and add-feature/fix-bug). fix-security, which
        # doesn't write tests, must NOT carry them. Marker: the hand-rolled-app
        # prohibition is unique to that layer.
        wt = compose_system_prompt(skill_for("test", "testing"))
        fs = compose_system_prompt(skill_for("guardrail", "injection"))
        assert "hand-rolled FastAPI" in wt
        assert "hand-rolled FastAPI" not in fs

    def test_guardrail_honesty_only_in_fix_security(self):
        wt = compose_system_prompt(skill_for("test", "testing"))
        fs = compose_system_prompt(skill_for("guardrail", "injection"))
        assert "GUARDRAIL HONESTY" in fs
        assert "GUARDRAIL HONESTY" not in wt

    def test_new_deps_special_only_in_add_feature(self):
        # The NEW-package special operation is add-feature-specific…
        af = compose_system_prompt(skill_for("milestone", "feature"))
        fs = compose_system_prompt(skill_for("guardrail", "injection"))
        assert "NEW DEPS ARE A SPECIAL OPERATION" in af
        assert "NEW DEPS ARE A SPECIAL OPERATION" not in fs

    def test_dependency_honesty_is_universal(self):
        # …but plain dependency-honesty (any import must be manifest-backed) is
        # now in BASE, so EVERY shape carries it (review finding: a test/fix/
        # security patch that adds an import was losing it).
        for kind, category in _ALL_CASES:
            p = compose_system_prompt(skill_for(kind, category))
            assert "DEPENDENCY HONESTY" in p, f"{kind}/{category} lost dependency honesty"

    def test_no_new_sinks_is_universal(self):
        # The eval/exec/shell prohibition was only in fix-security; it's now in
        # BASE since the post-lint enforces it for every shape.
        for kind, category in _ALL_CASES:
            p = compose_system_prompt(skill_for(kind, category))
            assert "NO NEW DYNAMIC-EXECUTION SINKS" in p, f"{kind}/{category} lost no-new-sinks"


class TestTestQualityLayer:
    """Review findings (high): fix-bug and add-feature are told they MAY add a
    test, so they must carry the no-theater / placement rules too — not only
    write-test."""

    @pytest.mark.parametrize("kind,category", [
        ("test", "testing"),         # write-test
        ("milestone", "feature"),    # add-feature (preamble anticipates tests)
        ("next_action", "bug"),      # fix-bug (its clause C adds a regression test)
    ])
    def test_test_writing_shapes_carry_quality_rules(self, kind, category):
        p = compose_system_prompt(skill_for(kind, category))
        assert "hand-rolled FastAPI" in p          # no-theater
        assert "backend/tests/" in p                # placement
        assert "status_code in [200, 401, 404, 405]" in p

    def test_fix_security_does_not_carry_test_rules(self):
        # fix-security doesn't write tests, so it shouldn't carry the layer
        # (keeps its prompt focused — the whole point of decomposition).
        p = compose_system_prompt(skill_for("guardrail", "injection"))
        assert "hand-rolled FastAPI" not in p


# ── gate_kind ↔ sandbox.gate_for consistency (Phase 2 + 3 single source) ─────

class TestGateConsistency:
    @pytest.mark.parametrize("kind,category", [
        ("test", "testing"), ("guardrail", "injection"),
        ("milestone", "ci_cd"), ("milestone", "feature"),
        ("blocker", "refactor"), ("next_action", "bug"),
    ])
    def test_skill_gate_kind_matches_sandbox_tier(self, kind, category):
        # sandbox.gate_for derives its tier from the matched skill's gate_kind
        # (refactored in Phase 3) — so for any non docs/deps finding they agree.
        skill = skill_for(kind, category)
        tier = sandbox.gate_for(kind, category)
        assert tier.name == skill.gate_kind, (
            f"{kind}/{category}: skill says {skill.gate_kind}, gate_for says {tier.name}"
        )


# ── system_prompt_for (the agent-facing entry point) ─────────────────────────

class TestSystemPromptFor:
    def test_uses_skill_for_brief_kind(self):
        brief = CoderBrief(
            task="t", repo_full_name="o/r",
            finding_kind="guardrail", finding_id="SEC-1", finding_category="injection",
        )
        p = system_prompt_for(brief)
        assert "GUARDRAIL HONESTY" in p          # fix-security fragment
        assert "NO TEST THEATER" not in p

    def test_diff_mode_appends_override(self):
        brief = CoderBrief(
            task="t", repo_full_name="o/r",
            finding_kind="test", finding_id="T-1", finding_category="testing",
        )
        p = system_prompt_for(brief, diff_mode=True)
        assert "UNIFIED DIFF MODE" in p
        assert "hand-rolled FastAPI" in p         # write-test skill fragment still present

    def test_fails_open_to_monolith(self, monkeypatch):
        # If the skills package blows up, fall back to the legacy monolith so the
        # Coder always has a valid prompt.
        import app.agents.coder_agent as ca

        def boom(*a, **k):
            raise RuntimeError("skills broke")

        monkeypatch.setattr(ca, "system_prompt_for", system_prompt_for)  # ensure real fn
        monkeypatch.setattr("app.services.skills.skill_for", boom)
        brief = CoderBrief(
            task="t", repo_full_name="o/r",
            finding_kind="guardrail", finding_id="SEC-1", finding_category="injection",
        )
        p = system_prompt_for(brief)
        assert p == _SYSTEM_PROMPT

    def test_unknown_kind_brief_gets_base(self):
        brief = CoderBrief(
            task="t", repo_full_name="o/r",
            finding_kind="mystery", finding_id="X-1",
        )
        p = system_prompt_for(brief)
        assert "## Skill:" not in p               # base only
        assert "VERIFY:" in p
