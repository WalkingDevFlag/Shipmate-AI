"""
Deterministic kind+category → skill dispatch. No LLM router — a pure function
so the chosen skill is reproducible, testable, and free.

Dispatch precedence (first match wins), aligned with the orchestrator's
existing kind/category vocabulary (_SMART_KINDS / _SMART_CATEGORIES and the
_resolve_target_paths category mapping):

  1. kind == 'test'                          → write-test
  2. category == 'testing'                   → write-test
  3. kind == 'guardrail'                     → fix-security  (incl. its 'config'
                                               hygiene category)
  4. category in security set                → fix-security  ('secrets', 'auth',
                                               'cors', 'injection', 'security')
  5. category == 'ci_cd'                     → fix-ci
  6. category == 'refactor'                  → refactor
  7. category == 'bug'                       → fix-bug
  8. kind in {milestone,blocker,next_action} → add-feature
  9. anything else (unknown)                 → BASE only (fail-safe floor)

Two category subtleties the review surfaced:
  • 'security' is a first-class PlanForge/opportunity milestone category (it
    reaches the Coder as kind=milestone/blocker/next_action, category=security),
    so it MUST route to fix-security — it's included in the security set.
  • 'config' is OVERLOADED: GuardRail uses category='config' for a security
    hygiene concern, but RepoLens/milestones use it for a non-security
    structural config edit. So 'config' routes to fix-security ONLY when
    kind=='guardrail' (the security source); a non-guardrail config finding
    falls through to its kind's skill. 'config' is therefore NOT in the
    unconditional security category set.
"""
from __future__ import annotations

from typing import Dict

from .base import BASE_SKILL, Skill
from .catalog import (
    ADD_FEATURE,
    FIX_BUG,
    FIX_CI,
    FIX_SECURITY,
    REFACTOR,
    SKILLS,
    WRITE_TEST,
)

# name -> Skill, including base, for lookup/iteration.
ALL_SKILLS: Dict[str, Skill] = {s.name: s for s in [BASE_SKILL, *SKILLS]}

# Security/hygiene categories that UNCONDITIONALLY route to fix-security.
# 'security' is the PlanForge/opportunity milestone category for security work;
# the rest mirror the orchestrator's _SMART_CATEGORIES. 'config' is NOT here —
# it's overloaded (GuardRail hygiene vs structural config), so it routes to
# fix-security only under kind=='guardrail' (handled below). 'ci_cd' has its
# own skill.
_SECURITY_CATEGORIES = frozenset({"secrets", "auth", "cors", "injection", "security"})
_FEATURE_KINDS = frozenset({"milestone", "blocker", "next_action"})


def skill_for(kind: str, category: str = "") -> Skill:
    """Pick the one skill for a finding. Deterministic + fail-safe: an
    unrecognised shape returns BASE_SKILL (universal rules only), so the Coder
    always has a valid prompt — never a crash, never an empty prompt."""
    k = (kind or "").lower().strip()
    c = (category or "").lower().strip()

    # Testing first — a 'test' kind or 'testing' category is unambiguous and
    # should never be diluted by a feature/security fragment.
    if k == "test" or c == "testing":
        return WRITE_TEST

    # Security/hygiene. kind 'guardrail' is always security (incl. its 'config'
    # hygiene category). For other kinds, only the unconditional security
    # categories route here — NOT a bare 'config', which is structural there.
    if k == "guardrail" or c in _SECURITY_CATEGORIES:
        return FIX_SECURITY

    # CI/CD workflow edits.
    if c == "ci_cd":
        return FIX_CI

    # Explicit refactor / bug categories.
    if c == "refactor":
        return REFACTOR
    if c == "bug":
        return FIX_BUG

    # Feature/milestone work (the broad default for those kinds).
    if k in _FEATURE_KINDS:
        return ADD_FEATURE

    # Unknown shape — universal rules only (fail-safe). The Coder still gets a
    # complete, valid prompt; it just carries no kind-specific section.
    return BASE_SKILL
