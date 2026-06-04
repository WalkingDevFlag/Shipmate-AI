"""Typed agent identifiers using StrEnum for type safety and IDE autocomplete."""

from enum import StrEnum


class AgentKey(StrEnum):
    """Enumeration of all available agent identifiers."""

    REPO_LENS = "repo_lens"
    PLAN_FORGE = "plan_forge"
    GUARDRAIL = "guardrail"
    TESTPILOT = "testpilot"
