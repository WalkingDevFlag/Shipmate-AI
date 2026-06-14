"""
Coder skill registry (Phase 3).

The Coder agent's behaviour used to live in ONE monolithic ~130-line system
prompt holding 10 hard rules — but most rules are kind-specific noise for any
given finding: the test-theater rules only matter when writing tests, the
guardrail-honesty rule only for security, the new-dependency rules only for
manifest work. A guardrail patch carried the full test-writing lecture and
vice-versa, diluting the rules that actually applied.

A SKILL decomposes that monolith:

  • a BASE skill — the universal rules every patch must honour (import fidelity,
    scope discipline, wire-it-up, contract stability, the VERIFY self-check);
  • one skill PER finding shape — write-test, fix-security, add-feature, fix-ci,
    refactor, fix-bug — carrying only the guidance that shape needs.

`skill_for(kind, category)` is a DETERMINISTIC dispatch (no LLM router): the
finding's kind+category map to exactly one skill. `compose_system_prompt(skill)`
= the shared preamble + base rules + that one skill's fragment. The result is
shorter and sharper than the monolith for any single finding, which both
improves rule adherence and frees prompt budget for the repo map (Phase 1).

Compounding wins this unlocks (later phases / already-present subsystems):
  • coder_lessons can be keyed PER SKILL ("this repo's write-test skill keeps
    doing X") instead of one flat per-repo bucket;
  • golden tests can assert per-skill prompt properties;
  • Phase 2's gate_for reuses each skill's `gate_kind` so the validation tier is
    declared next to the behaviour it validates.

Everything is pure + fail-open: an unknown kind falls back to the base skill
alone (never crashes an actuate), and compose_system_prompt always at least
returns the base rules.
"""
from __future__ import annotations

from .base import BASE_SKILL, Skill, compose_system_prompt
from .registry import ALL_SKILLS, skill_for

__all__ = [
    "Skill",
    "BASE_SKILL",
    "ALL_SKILLS",
    "skill_for",
    "compose_system_prompt",
]
