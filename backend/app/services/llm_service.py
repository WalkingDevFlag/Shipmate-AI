"""
LLM enhancement layer.

The 4 agents (RepoLens, PlanForge, GuardRail, TestPilot) compute deterministic
heuristic outputs first — file counts, has_tests flags, score formulas. After
that they hand the result to `LLMService.enhance(...)`, which optionally calls
an LLM to **rewrite the prose fields** (milestone descriptions, blocker
resolutions, security recommendations, suggested-test prose) so they read as
specific to *this* repo instead of generic templates.

What's enhanced (per agent):
  - PlanForge:  milestones[].description, blockers[].resolution,
                next_best_action, dependencies (order/clarity).
  - GuardRail:  findings[].description, findings[].recommendation.
  - TestPilot:  suggested_tests[].description, missing_coverage_areas.
  - RepoLens:   (skipped — every field is heuristic, no template prose).

What's NEVER touched:
  - Numeric scores (repo_score, delivery_score, security_score, test_score).
  - Booleans (has_tests, has_ci_cd, has_dockerfile).
  - Counts (file_count, test count, finding count).
  - Field shapes — we only update existing fields, never add/remove.

If the LLM provider is unavailable, the call fails, or the response can't be
parsed, `enhance(...)` returns `base_output` unchanged. The deterministic
score and verdict are always present even with no Bedrock access.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import threading
from typing import Any, Dict, List, Optional, Type, TypeVar

from pydantic import BaseModel, Field

from app.schemas.agent_schemas import (
    Blocker, GuardRailOutput, Milestone, Opportunity, PlanForgeOutput,
    SecurityFinding, Severity, SuggestedTest, TestPilotOutput,
)

logger = logging.getLogger("shipmate.llm_service")

# ── Provider singleton ──────────────────────────────────────────────────────
# Lazily created on first use; cached for the process lifetime. The provider
# itself logs init failures (e.g. expired ADA creds) and falls back to None
# so subsequent enhance() calls cheaply short-circuit.
_provider: Optional[Any] = None
_provider_init_attempted = False
# The agents run via asyncio.to_thread, so _get_provider() / _maybe_invalidate
# can be entered from multiple worker THREADS concurrently. A plain
# check-then-set on the module globals is a TOCTOU race that can construct the
# provider (and its boto3/Azure client) twice. A threading.Lock — NOT an
# asyncio.Lock, because these call sites are synchronous — serializes init and
# invalidation. Double-checked locking keeps the hot path lock-free once the
# provider is resolved.
_provider_lock = threading.Lock()


def _get_provider():
    """Return the configured LLM provider singleton, or None if unavailable.

    Routed through the factory so the same flag (SHIPMATE_LLM_PROVIDER, or the
    legacy LLM_PROVIDER) selects Bedrock or Azure OpenAI. Unlike the agents'
    hard get_provider() (which raises), the enhancement layer stays
    None-tolerant: any construction failure (missing creds, missing SDK)
    disables prose enhancement gracefully — deterministic scores/verdicts are
    unaffected.

    Thread-safe: double-checked locking so concurrent worker threads can't
    construct two providers."""
    global _provider, _provider_init_attempted
    if _provider_init_attempted:
        return _provider
    with _provider_lock:
        # Re-check inside the lock: another thread may have initialized while
        # we waited to acquire it.
        if _provider_init_attempted:
            return _provider
        try:
            from app.services.llm_provider import get_provider, provider_kind
            _provider = get_provider()
            logger.info("LLMService: %s provider ready", provider_kind())
        except Exception as e:
            logger.warning("LLMService: provider init failed (%s); enhancements disabled", e)
            _provider = None
        _provider_init_attempted = True
    return _provider


# Markers in error strings that mean "AWS creds rolled over mid-process".
# When we see one, reset the provider singleton so the NEXT call rebuilds
# the boto3 client and picks up freshly-refreshed ADA creds at
# ~/.aws/credentials — no backend restart needed.
_TRANSIENT_AUTH_MARKERS = (
    "ExpiredToken",
    "ExpiredTokenException",
    "InvalidSignatureException",
    "UnrecognizedClientException",
    "Signature expired",
)


def _maybe_invalidate_provider(err: BaseException) -> None:
    """Reset the cached provider on auth-class errors so the next request retries fresh."""
    global _provider, _provider_init_attempted
    msg = str(err)
    if any(m in msg for m in _TRANSIENT_AUTH_MARKERS):
        logger.warning(
            "LLMService: detected transient auth failure (%s) — invalidating "
            "provider cache. The next request rebuilds the LLM client with "
            "freshly-resolved credentials, so a refreshed key/token takes "
            "effect without a restart.",
            type(err).__name__,
        )
        # Reset under the same lock _get_provider uses, so an invalidation can't
        # race a concurrent re-init into an inconsistent (attempted=True,
        # provider=None-but-being-built) state.
        with _provider_lock:
            _provider = None
            _provider_init_attempted = False
        # Also drop the factory's cached singleton so the rebuild actually
        # constructs a fresh client (the factory is what we now build through).
        try:
            from app.services.llm_provider import reset_provider
            reset_provider()
        except Exception:
            pass


# ── Per-agent enhancement schemas ────────────────────────────────────────────
# These are *partial* projections of the agent output schemas. Bedrock fills
# only these prose-y fields; we then merge them back into the heuristic
# base_output via Pydantic `model_copy(update=...)`.

class _MilestoneEnhancement(BaseModel):
    title: str = Field(..., description="Same milestone title from the input plan, repeated verbatim.")
    description: str = Field(..., description="2-sentence repo-specific description of what to do and why it matters here.")


class _BlockerEnhancement(BaseModel):
    id: str = Field(..., description="Blocker id from input, verbatim.")
    resolution: str = Field(..., description="One-paragraph concrete resolution tailored to this repo's tech stack and current state.")


class PlanForgeEnhancement(BaseModel):
    """Prose-only fields PlanForge can have rewritten."""
    milestones: List[_MilestoneEnhancement] = Field(..., description="Same length as input milestones; same titles in same order.")
    blockers: List[_BlockerEnhancement] = Field(..., description="Same length as input blockers; same ids in same order.")
    next_best_action: str = Field(..., description="One actionable sentence — what to do RIGHT NOW given the repo state.")


class _FindingEnhancement(BaseModel):
    id: str = Field(..., description="Finding id from input, verbatim.")
    description: str = Field(..., description="2-3 sentences explaining the actual risk in this repo.")
    recommendation: str = Field(..., description="2-3 concrete steps the team should take to fix it.")


class GuardRailEnhancement(BaseModel):
    """Prose-only fields GuardRail can have rewritten."""
    findings: List[_FindingEnhancement] = Field(..., description="Same length as input findings; same ids in same order.")


class _SuggestedTestEnhancement(BaseModel):
    name: str = Field(..., description="Test name from input, verbatim.")
    description: str = Field(..., description="2 sentences: what this test should cover and why, given the repo's stack.")


class TestPilotEnhancement(BaseModel):
    """Prose-only fields TestPilot can have rewritten."""
    suggested_tests: List[_SuggestedTestEnhancement] = Field(..., description="Same length and order as input.")
    missing_coverage_areas: List[str] = Field(..., description="Same length and order as input; rephrase each item to be specific to this repo.")


# ── Prompt builders ─────────────────────────────────────────────────────────

def _repo_summary(context: Dict[str, Any]) -> str:
    """A compact text blob describing the repo for the system prompt."""
    info = context.get("repo_info") or {}
    repo_lens = context.get("repo_lens")

    parts = [
        f"Repo: {info.get('full_name', '?')}",
        f"Default branch: {context.get('branch', 'main')}",
    ]
    if info.get("description"):
        parts.append(f"Description: {info['description']}")
    if repo_lens is not None:
        # repo_lens is a Pydantic model
        parts.append(f"Tech stack: {', '.join(repo_lens.tech_stack) or '(unknown)'}")
        parts.append(f"Primary language: {repo_lens.primary_language}")
        parts.append(f"Architecture pattern: {repo_lens.architecture_pattern}")
        parts.append(f"Has CI/CD: {repo_lens.has_ci_cd} · Dockerfile: {repo_lens.has_dockerfile} · Tests: {repo_lens.has_tests}")
        parts.append(f"File count: {repo_lens.file_count}")
        if repo_lens.key_modules:
            parts.append(f"Key modules: {', '.join(repo_lens.key_modules[:8])}")
        if repo_lens.entry_points:
            parts.append(f"Entry points: {', '.join(repo_lens.entry_points[:5])}")
        if repo_lens.architecture_risks:
            risks = "; ".join(r.risk for r in repo_lens.architecture_risks[:5])
            parts.append(f"Architecture risks: {risks}")
    feature_ctx = context.get("feature_context")
    if feature_ctx:
        parts.append(f"Feature in scope: {feature_ctx[:300]}")
    return "\n".join(parts)


_AGENT_SYSTEM_PROMPTS: Dict[str, str] = {
    "plan_forge": (
        "You are a senior staff engineer reviewing a repo's release readiness. "
        "Given a deterministic delivery plan (milestones + blockers + a next-best-action), "
        "rewrite the PROSE so each item is concrete and specific to *this* repo's tech "
        "stack, file layout, and current state. Preserve every milestone title and blocker "
        "id verbatim. Do not invent new milestones or blockers."
    ),
    "guardrail": (
        "You are an application-security engineer reviewing findings from a static analysis pass. "
        "For each finding, rewrite the description so it explains the actual risk in *this* repo, "
        "and rewrite the recommendation as 2-3 concrete remediation steps. Preserve every finding "
        "id verbatim. Do not invent new findings or change severities."
    ),
    "testpilot": (
        "You are a QA lead. Given suggested tests and a list of coverage gaps, rewrite the prose "
        "so each test description and gap is specific to this repo's stack and entry points. "
        "Preserve every test name verbatim. Do not invent new tests; do not remove items."
    ),
}


def _user_prompt(agent_name: str, context: Dict[str, Any], base_output_dict: Dict[str, Any]) -> str:
    return (
        f"# Repo summary\n{_repo_summary(context)}\n\n"
        f"# Current {agent_name} output (rewrite the PROSE fields only):\n"
        f"{json.dumps(base_output_dict, indent=2)[:6000]}\n\n"
        f"Return ONLY the requested enhancement schema. Match item ids/titles exactly."
    )


# ── Core enhance() — sync wrapper around the async provider call ────────────

T = TypeVar("T", bound=BaseModel)

_AGENT_TO_SCHEMA: Dict[str, Type[BaseModel]] = {
    "plan_forge": PlanForgeEnhancement,
    "guardrail":  GuardRailEnhancement,
    "testpilot":  TestPilotEnhancement,
}


def _merge_plan_forge(base: PlanForgeOutput, enh: PlanForgeEnhancement) -> PlanForgeOutput:
    # Match enriched milestones/blockers back by title/id, preserving order + length.
    milestone_map = {m.title: m.description for m in enh.milestones}
    blocker_map = {b.id: b.resolution for b in enh.blockers}
    new_milestones = [m.model_copy(update={"description": milestone_map.get(m.title, m.description)})
                       for m in base.milestones]
    new_blockers = [b.model_copy(update={"resolution": blocker_map.get(b.id, b.resolution)})
                     for b in base.blockers]
    return base.model_copy(update={
        "milestones": new_milestones,
        "blockers": new_blockers,
        "next_best_action": enh.next_best_action or base.next_best_action,
    })


def _merge_guardrail(base: GuardRailOutput, enh: GuardRailEnhancement) -> GuardRailOutput:
    finding_map = {f.id: (f.description, f.recommendation) for f in enh.findings}
    new_findings = []
    for f in base.findings:
        upd = finding_map.get(f.id)
        if upd:
            new_findings.append(f.model_copy(update={"description": upd[0], "recommendation": upd[1]}))
        else:
            new_findings.append(f)
    return base.model_copy(update={"findings": new_findings})


def _merge_testpilot(base: TestPilotOutput, enh: TestPilotEnhancement) -> TestPilotOutput:
    desc_map = {t.name: t.description for t in enh.suggested_tests}
    new_tests = [t.model_copy(update={"description": desc_map.get(t.name, t.description)})
                 for t in base.suggested_tests]
    new_gaps = list(enh.missing_coverage_areas) if enh.missing_coverage_areas else base.missing_coverage_areas
    return base.model_copy(update={
        "suggested_tests": new_tests,
        "missing_coverage_areas": new_gaps,
    })


_MERGERS = {
    "plan_forge": _merge_plan_forge,
    "guardrail":  _merge_guardrail,
    "testpilot":  _merge_testpilot,
}


class LLMService:
    """Static helpers that 3 agents call after their heuristic compute."""

    @classmethod
    def is_available(cls) -> bool:
        """Whether the LLM provider is currently constructible. Reflects the
        LIVE provider state (not a one-time cache) so that after a transient
        auth failure invalidates the provider — or after creds are refreshed —
        the answer is current. _get_provider() does its own per-process caching,
        so this stays cheap."""
        return _get_provider() is not None

    @classmethod
    def provider(cls):
        """The live LLM provider singleton (or None). Public accessor for
        callers outside this module that need to pass the provider into a critic
        pass (e.g. opportunity_critic.verify_opportunities). None-tolerant."""
        return _get_provider()

    @classmethod
    def enhance(cls, agent_name: str, context: Dict[str, Any], base_output: T) -> T:
        """
        Optionally rewrite prose fields on `base_output` using the configured
        LLM provider. Synchronous — agents are sync. On any failure, returns
        `base_output` unchanged.
        """
        provider = _get_provider()
        if provider is None:
            return base_output

        schema = _AGENT_TO_SCHEMA.get(agent_name)
        merger = _MERGERS.get(agent_name)
        if schema is None or merger is None:
            return base_output

        system = _AGENT_SYSTEM_PROMPTS[agent_name]
        user = _user_prompt(agent_name, context, base_output.model_dump())

        try:
            enhancement = provider.invoke_structured_sync(
                system_prompt=system,
                user_prompt=user,
                schema_class=schema,
                deployment_hint="smart" if agent_name == "guardrail" else "fast",
            )
        except Exception as e:
            logger.warning("LLM enhance failed for %s: %s; using base output", agent_name, e)
            _maybe_invalidate_provider(e)
            return base_output

        try:
            return merger(base_output, enhancement)
        except Exception as e:
            logger.warning("LLM enhance merge failed for %s: %s; using base output", agent_name, e)
            return base_output

    # Kept for backward compatibility with the old async stub signature, in
    # case something starts importing it later.
    @classmethod
    async def enhance_analysis(cls, agent_name: str, context: dict, base_output: dict) -> dict:
        return base_output

    # ── Discovery (Bedrock invents NEW items grounded in real code) ─────────

    @classmethod
    def discover_plan_forge(
        cls, context: Dict[str, Any], base: PlanForgeOutput,
    ) -> PlanForgeOutput:
        """
        Ask Bedrock to invent up to 5 new milestones AND up to 3 new blockers
        that aren't covered by the heuristic base. Each must cite which files
        in the repo motivated it (rationale field). Output is merged onto base.

        On any failure: returns `base` unchanged. Numeric scores untouched.
        """
        provider = _get_provider()
        if provider is None:
            logger.warning(
                "PlanForge discovery skipped: LLM provider unavailable "
                "(LLM_PROVIDER=%r, AWS creds may be expired). "
                "Refresh ADA + restart the backend to enable AI discovery.",
                os.getenv("LLM_PROVIDER", "(unset)"),
            )
            return base

        try:
            # Wider corpus + capability digest + journaled exclusions — same
            # recurrence defenses the Opportunity (Build) path already has. The
            # narrow 5-file blob + no digest is why milestones kept recurring in
            # Reports→PlanForge: the model never saw what already exists and had
            # no memory of what it proposed before.
            code_blob = _repo_code_blob(
                context, max_files=8,
                prefer=("routes", "main", "api", "service", "agent",
                        "orchestrator", "components", "pages", "hooks", "lib"),
            )
            capability_digest = _capability_digest(context)
            exclude_titles = _journaled_milestone_titles(context)
            user = _user_prompt_plan_discovery(
                context, base, code_blob,
                capability_digest=capability_digest, exclude_titles=exclude_titles,
            )
            discovery = provider.invoke_structured_sync(
                system_prompt=_DISCOVERY_PLAN_SYSTEM,
                user_prompt=user,
                schema_class=PlanForgeDiscovery,
                deployment_hint="smart",  # discovery is the slow + smart pass
            )
            merged = _merge_plan_discovery(base, discovery)
            # Suppress dismissed/shipped MILESTONES (the recurring ones the user
            # sees) AND blockers. Both flow through the journal/actuate path.
            # Distinct kinds so a milestone and a blocker with the same title get
            # separate journal rows.
            merged.milestones = cls._refine_findings(
                merged.milestones, "milestone", context, code_blob, provider,
            )
            merged.blockers = cls._refine_findings(
                merged.blockers, "blocker", context, code_blob, provider,
            )
            return merged
        except Exception as e:
            logger.warning("PlanForge discovery failed (%s); using base output", e)
            _maybe_invalidate_provider(e)
            return base

    @classmethod
    def discover_guardrail(
        cls, context: Dict[str, Any], base: GuardRailOutput,
    ) -> GuardRailOutput:
        """
        Ask Bedrock to read the auth/CORS/secrets-handling code and invent
        security findings that the regex ruleset misses. Each finding cites
        the file + reasoning. Merged onto base.
        """
        provider = _get_provider()
        if provider is None:
            logger.warning(
                "GuardRail discovery skipped: LLM provider unavailable "
                "(LLM_PROVIDER=%r, AWS creds may be expired). "
                "Refresh ADA + restart the backend to enable AI discovery.",
                os.getenv("LLM_PROVIDER", "(unset)"),
            )
            # Even with no LLM, still apply journal suppression (it's local +
            # free) so dismissed findings don't recur in pure-heuristic mode,
            # and still record/diff the capability posture (deterministic).
            base.findings = cls._refine_findings(
                base.findings, "guardrail", context, "", None,
            )
            base.findings = cls._apply_capability_drift(context, base.findings)
            return base

        try:
            code_blob = _repo_code_blob(
                context, max_files=6,
                prefer=("auth", "cors", "main", "security", ".env", "config", "routes"),
            )
            # Recurrence-aware (same as PlanForge/Opportunity discovery): tell the
            # LLM what already exists (capability digest) and what's already
            # shipped/dismissed (journaled guardrail+blocker titles) so it stops
            # re-inventing fixed findings under new wording.
            capability_digest = _capability_digest(context)
            exclude_titles = _journaled_titles(context, ("guardrail::", "blocker::"))
            user = _user_prompt_guardrail_discovery(
                context, base, code_blob,
                capability_digest=capability_digest, exclude_titles=exclude_titles,
            )
            discovery = provider.invoke_structured_sync(
                system_prompt=_DISCOVERY_GUARDRAIL_SYSTEM,
                user_prompt=user,
                schema_class=GuardRailDiscovery,
                deployment_hint="smart",
            )
            merged = _merge_guardrail_discovery(base, discovery)
            # Critic + journal suppression on the COMPLETE finding set (heuristic
            # + LLM-discovered), judged against the same code_blob the agent saw.
            merged.findings = cls._refine_findings(
                merged.findings, "guardrail", context, code_blob, provider,
            )
            # Capability posture snapshot + drift (B3): persist which controls
            # exist this run and, if a control PRESENT before is now GONE, surface
            # that regression as a real finding instead of silently not-detecting
            # it. Best-effort — never let posture tracking break discovery.
            merged.findings = cls._apply_capability_drift(context, merged.findings)
            return merged
        except Exception as e:
            logger.warning("GuardRail discovery failed (%s); using base output", e)
            _maybe_invalidate_provider(e)
            return base

    @staticmethod
    def _refine_findings(findings, kind, context, code_blob, provider):
        """Shared post-processing for a finding list:
          1. drop EXACT-signature journal-suppressed (dismissed/shipped) findings;
          2. drop SEMANTIC rephrases of shipped/dismissed findings (B1) — this is
             the architectural fix for GuardRail re-wording the same finding to
             mint a fresh signature every run;
          3. run the critic verifier against the exact code that was analyzed.
        All three fail-open — a failure here returns the findings unchanged
        rather than hiding anything."""
        try:
            from app.services import finding_critic as fc
            repo_full = (context.get("repo_info") or {}).get("full_name", "") or ""
            findings = fc.filter_suppressed(findings, kind, repo_full)
            # Semantic dedup against memory of shipped/dismissed findings —
            # catches a rephrase the exact-signature pass above misses.
            try:
                from app.services import finding_memory as fm
                findings = fm.filter_semantic_duplicates(findings, repo_full)
            except Exception as e:  # pragma: no cover - best-effort
                logger.debug("semantic dedup skipped (%s)", e)
            findings = fc.verify_findings(findings, code_blob, provider)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("_refine_findings failed (%s); keeping findings as-is", e)
        return findings

    @staticmethod
    def _apply_capability_drift(context: Dict[str, Any], findings):
        """Record this run's detected security-control set (B3) and, if a control
        present in a PRIOR run is now absent, append a 'control regressed'
        finding so the regression is surfaced rather than silently not-detected.
        Fail-open: any error returns findings unchanged."""
        try:
            from app.services import capability_store as cs
            key_files = context.get("key_files") or {}
            file_tree = context.get("file_tree") or []
            controls = _detect_security_controls(key_files, file_tree)
            drift = cs.record_snapshot(context, controls)
            seeds = cs.regression_findings(drift)
            if not seeds:
                return findings
            # Build SecurityFinding objects for each regressed control and prepend
            # them (regressions are high-signal — show them first). Continue the
            # EXISTING SEC-id sequence (parse the max id already minted by the
            # heuristic + discovery passes) so ids stay monotonic and unique —
            # a hard-coded 900 base would gap the sequence and could collide with
            # a later discovery pass that also climbs past it.
            next_id = 1
            for f in findings:
                fid = getattr(f, "id", "") or ""
                if fid.startswith("SEC-"):
                    try:
                        next_id = max(next_id, int(fid.split("-")[1]) + 1)
                    except (ValueError, IndexError):
                        pass
            regressions = []
            for seed in seeds:
                regressions.append(SecurityFinding(
                    id=f"SEC-{next_id:03d}",
                    title=seed["title"],
                    severity=Severity.HIGH,
                    category="config",
                    description=seed["description"],
                    recommendation=(
                        "Confirm whether removing this control was intentional. If "
                        "not, restore it; if intentional, document why the posture "
                        "changed."
                    ),
                    source="discovery",
                    rationale=f"capability drift: control '{seed['control']}' no longer detected",
                ))
                next_id += 1
            return regressions + list(findings)
        except Exception as e:  # pragma: no cover - best-effort
            logger.debug("_apply_capability_drift failed (%s); skipping", e)
            return findings

    @classmethod
    def discover_testpilot(
        cls, context: Dict[str, Any], base: TestPilotOutput,
    ) -> TestPilotOutput:
        """
        Read the actual entry-point and route code, invent test cases that
        target REAL functions/endpoints/edge cases instead of generic
        templates (test_auth_flows, test_e2e_happy_path, etc.). Each
        suggested test cites the file/function it targets. Merged onto base.
        """
        provider = _get_provider()
        if provider is None:
            logger.warning(
                "TestPilot discovery skipped: LLM provider unavailable "
                "(LLM_PROVIDER=%r). Tests will be the static template list.",
                os.getenv("LLM_PROVIDER", "(unset)"),
            )
            return base

        try:
            code_blob = _repo_code_blob(
                context, max_files=6,
                prefer=("routes", "main", "api", "service", "agent", "handler"),
            )
            user = _user_prompt_testpilot_discovery(context, base, code_blob)
            discovery = provider.invoke_structured_sync(
                system_prompt=_DISCOVERY_TESTPILOT_SYSTEM,
                user_prompt=user,
                schema_class=TestPilotDiscovery,
                deployment_hint="smart",
            )
            return _merge_testpilot_discovery(base, discovery)
        except Exception as e:
            logger.warning("TestPilot discovery failed (%s); using base output", e)
            _maybe_invalidate_provider(e)
            return base

    # ── Opportunity discovery (Phase 1A — self-improvement work for the repo) ──

    @staticmethod
    def opportunity_code_blob(context: Dict[str, Any]) -> str:
        """The exact code blob shown to the opportunity discoverer. Exposed so
        the 1B critic (verify_opportunities) can judge against the SAME evidence
        the discoverer saw — same contract as finding_critic using the agent's
        code_blob."""
        return _repo_code_blob(
            context, max_files=8,
            prefer=("routes", "main", "api", "service", "agent",
                    "orchestrator", "components", "pages", "hooks", "lib"),
        )

    @classmethod
    def discover_opportunities(
        cls, context: Dict[str, Any], max_opportunities: int = 8,
        *, exclude_titles: Optional[List[str]] = None,
    ) -> List[Opportunity]:
        """Read the repo's actual code and propose a BALANCED set of
        self-improvement opportunities (features / improvements / tweaks / bugs),
        each grounded in cited files. This is the PlanForge-enrichment pass
        re-aimed at "what should we build next" rather than "what blocks
        shipping". Returns raw (un-ranked, un-suppressed) Opportunity objects;
        the OpportunityService applies the critic + ranker + journal join.

        `exclude_titles`: opportunities already shipped/dismissed/in-flight (from
        the journal) — fed to the prompt as DO-NOT-PROPOSE so the stateless model
        stops re-surfacing them every run.

        Fail-open: returns [] if the provider is unavailable (callers treat an
        empty list as ai_enhanced=False, never as "repo is perfect")."""
        provider = _get_provider()
        if provider is None:
            logger.warning(
                "Opportunity discovery skipped: LLM provider unavailable "
                "(LLM_PROVIDER=%r). Refresh ADA + restart the backend.",
                os.getenv("LLM_PROVIDER", "(unset)"),
            )
            return []

        try:
            code_blob = cls.opportunity_code_blob(context)
            capability_digest = _capability_digest(context)
            user = _user_prompt_opportunity_discovery(
                context, code_blob, max_opportunities,
                capability_digest=capability_digest,
                exclude_titles=exclude_titles or [],
            )
            discovery = provider.invoke_structured_sync(
                system_prompt=_DISCOVERY_OPPORTUNITY_SYSTEM,
                user_prompt=user,
                schema_class=OpportunityDiscovery,
                deployment_hint="smart",
            )
            return _coerce_opportunities(discovery, max_opportunities)
        except Exception as e:
            logger.warning("Opportunity discovery failed (%s); returning none", e)
            _maybe_invalidate_provider(e)
            return []

    @classmethod
    def discover_innovations(
        cls, context: Dict[str, Any], max_opportunities: int = 6,
        *, exclude_titles: Optional[List[str]] = None,
    ) -> List[Opportunity]:
        """Sibling of `discover_opportunities`, INVERTED posture: instead of
        conservative product-review fixes, ask for AMBITIOUS, novel, sometimes-
        architectural ideas — new agent passes, new capabilities, research-grade
        directions — the kind the conservative discovery prompt explicitly tells
        the model NOT to emit ('speculative rewrites or future architecture').

        Same output shape (Opportunity) and same fail-open contract. The
        deterministic floor (must cite real files, must not already exist) is
        still enforced downstream by `innovation_critic` — novelty is rewarded,
        but groundlessness is not."""
        provider = _get_provider()
        if provider is None:
            logger.warning(
                "Innovation discovery skipped: LLM provider unavailable "
                "(LLM_PROVIDER=%r).", os.getenv("LLM_PROVIDER", "(unset)"),
            )
            return []
        try:
            code_blob = cls.opportunity_code_blob(context)
            capability_digest = _capability_digest(context)
            user = _user_prompt_innovation_discovery(
                context, code_blob, max_opportunities,
                capability_digest=capability_digest,
                exclude_titles=exclude_titles or [],
            )
            discovery = provider.invoke_structured_sync(
                system_prompt=_DISCOVERY_INNOVATION_SYSTEM,
                user_prompt=user,
                schema_class=OpportunityDiscovery,
                deployment_hint="smart",
            )
            return _coerce_opportunities(discovery, max_opportunities)
        except Exception as e:
            logger.warning("Innovation discovery failed (%s); returning none", e)
            _maybe_invalidate_provider(e)
            return []

    @classmethod
    def research_codebase(
        cls, context: Dict[str, Any], graph_summary: Dict[str, Any],
        *, question: str = "", max_findings: int = 8,
    ) -> Optional["_ResearchDiscovery"]:
        """Codebase deep-research pass: answer a question and/or surface
        dataflow-cleanup findings (dead code, cycles, god-modules, coupling),
        GROUNDED on the reference-graph summary so the model reasons about real
        edges, not guesses. Returns a _ResearchDiscovery (answer + findings) or
        None when the provider is unavailable (fail-open)."""
        provider = _get_provider()
        if provider is None:
            return None
        try:
            code_blob = cls.opportunity_code_blob(context)
            user = _user_prompt_research(
                context, code_blob, graph_summary,
                question=question, max_findings=max_findings,
            )
            disc = provider.invoke_structured_sync(
                system_prompt=_RESEARCH_SYSTEM,
                user_prompt=user,
                schema_class=_ResearchDiscovery,
                deployment_hint="smart",
            )
            return _salvage_research_findings(disc)
        except Exception as e:
            logger.warning("Codebase research failed (%s); returning none", e)
            _maybe_invalidate_provider(e)
            return None


# ─── Discovery — schemas ─────────────────────────────────────────────────────
# These are what Bedrock fills in. They're separate from PlanForgeOutput etc.
# because we don't want the LLM to redefine fields it shouldn't (scores,
# heuristic milestones, blocker counts).

class _DiscoveredMilestone(BaseModel):
    title: str = Field(..., description="Concise milestone title — 4-8 words.")
    description: str = Field(..., description="2-3 sentence description specific to this repo.")
    estimated_days: int = Field(..., ge=1, le=21, description="Realistic effort estimate in working days.")
    priority: str = Field(..., description="One of: critical, high, medium, low.")
    category: str = Field(..., description="One of: feature, testing, security, ci_cd, infra, docs.")
    rationale: str = Field(..., description="WHY this matters — cite at least one file path or code construct from the repo.")


class _DiscoveredBlocker(BaseModel):
    title: str = Field(..., description="Concrete blocker title.")
    description: str = Field(..., description="2-3 sentences on what's broken/missing in this repo.")
    severity: str = Field(..., description="One of: critical, high, medium.")
    resolution: str = Field(..., description="2-3 concrete steps to unblock.")
    category: str = Field(..., description="e.g. ci_cd, testing, security, docs, structure, deps, config.")
    rationale: str = Field(..., description="Cite specific files/constructs that prove this blocker exists.")


class PlanForgeDiscovery(BaseModel):
    """LLM-discovered improvements for THIS repo, grounded in actual code."""
    milestones: List[_DiscoveredMilestone] = Field(
        default_factory=list,
        description="Up to 5 NEW milestones. Each must be specific to this repo's code, not generic.",
    )
    blockers: List[_DiscoveredBlocker] = Field(
        default_factory=list,
        description="Up to 3 NEW blockers. Skip anything already in the heuristic base.",
    )


class _DiscoveredFinding(BaseModel):
    title: str = Field(..., description="Concise finding title — 4-8 words.")
    severity: str = Field(..., description="One of: critical, high, medium, low, info.")
    category: str = Field(..., description="One of: secrets, auth, cors, injection, deps, exposure, config.")
    description: str = Field(..., description="2-3 sentences explaining the actual risk in this repo.")
    recommendation: str = Field(..., description="2-3 concrete remediation steps.")
    file: Optional[str] = Field(None, description="Specific file path where the risk lives, if known.")
    rationale: str = Field(..., description="Cite the code pattern or construct that motivated this finding.")


class GuardRailDiscovery(BaseModel):
    """LLM-discovered security findings beyond the regex ruleset."""
    findings: List[_DiscoveredFinding] = Field(
        default_factory=list,
        description="Up to 5 NEW findings. Skip anything already detected by the heuristic ruleset.",
    )


class _DiscoveredTest(BaseModel):
    name: str = Field(..., description="snake_case test function name targeting a SPECIFIC function/endpoint in this repo (e.g. 'test_analyze_endpoint_handles_invalid_token').")
    type: str = Field(..., description="One of: unit, integration, e2e, security, performance.")
    priority: str = Field(..., description="One of: critical, high, medium, low.")
    description: str = Field(..., description="2 sentences: what this test exercises and why it catches a real failure mode in THIS code.")
    target_file: Optional[str] = Field(None, description="File path being tested (or where the test should live).")
    rationale: str = Field(..., description="Cite the exact function/endpoint/branch this test covers, and why heuristic-template tests would miss it.")


class TestPilotDiscovery(BaseModel):
    """LLM-discovered test cases grounded in this repo's actual code paths."""
    suggested_tests: List[_DiscoveredTest] = Field(
        default_factory=list,
        description="Up to 5 NEW tests. Each must target a real function/endpoint/edge-case in this repo, NOT a generic template (test_auth_flows, test_e2e_happy_path, etc).",
    )
    missing_coverage_areas: List[str] = Field(
        default_factory=list,
        description="Up to 3 specific code regions (file path + function/concern) currently uncovered. e.g. 'backend/app/orchestrator/shipmate_orchestrator.py — RepoLens enrichment merge logic'.",
    )


class _DiscoveredOpportunity(BaseModel):
    title: str = Field(..., description="Concise opportunity title — 4-9 words. e.g. 'Cache repo file tree across analyze runs'.")
    category: str = Field(..., description="One of: feature, improvement, tweak, bug.")
    description: str = Field(..., description="2-3 sentences: what this opportunity is, concretely, for THIS repo.")
    impact: str = Field(..., description="One sentence: what measurably gets better (UX, perf, reliability, coverage) if shipped.")
    effort: str = Field(..., description="Rough t-shirt size: one of S, M, L.")
    estimated_days: int = Field(..., ge=1, le=21, description="Realistic effort in working days.")
    target_files: List[str] = Field(default_factory=list, description="1-4 EXISTING file paths this work would touch. Use paths visible in the file tree / code shown.")
    suggested_approach: List[str] = Field(default_factory=list, description="2-4 high-level steps (NOT a full plan). e.g. ['add an in-memory LRU keyed by repo+branch', 'invalidate on push webhook'].")
    evidence: List[str] = Field(default_factory=list, description="1-3 SPECIFIC file paths or code constructs from the repo that prove this opportunity is real (e.g. 'backend/app/services/repo_analysis_service.py:_fetch_files — no caching').")
    rationale: str = Field(..., description="WHY this matters for THIS repo, citing at least one real file/construct from the code shown.")


class OpportunityDiscovery(BaseModel):
    """LLM-discovered self-improvement opportunities, grounded in real code."""
    opportunities: List[_DiscoveredOpportunity] = Field(
        default_factory=list,
        description="A BALANCED mix across feature/improvement/tweak/bug. Each MUST cite real files in evidence. Quality over quantity.",
    )


class _ResearchFinding(BaseModel):
    title: str = Field(..., description="Concise finding title — 4-9 words.")
    kind: str = Field(..., description="One of: dataflow, dead_code, coupling, risk, observation.")
    severity: str = Field(..., description="One of: high, medium, low.")
    detail: str = Field(..., description="2-3 sentences: what it is, concretely, for THIS repo.")
    evidence: List[str] = Field(default_factory=list, description="1-3 REAL file paths / constructs that prove it.")
    suggested_action: str = Field(default="", description="One concrete next step (NOT a full plan).")
    graph_signal: str = Field(default="", description="Which reference-graph signal backs this, verbatim (e.g. 'fan_in=11', 'cycle a<->b', 'unreferenced export: foo'). Empty if not graph-derived.")


class _ResearchDiscovery(BaseModel):
    """LLM codebase-research output: a narrative answer + grounded findings."""
    answer: str = Field(default="", description="Narrative answer to the asked question. Empty when no question was asked (open audit).")
    findings: List[_ResearchFinding] = Field(
        default_factory=list,
        description="Grounded loops/holes/tweaks/dataflow issues. Each MUST cite real files; prefer findings the reference-graph summary supports.",
    )


# Two shapes of the Bedrock multi-field forced-tool-call leak, where the model
# serializes the `findings` ARRAY into the `answer` STRING instead of the
# structured field: an XML param tag `<parameter name="findings">[...]>` or a
# bare JSON `"findings": [...]`. Both observed live on Opus-4.8/Bedrock for the
# two-field research schema (the single-list opportunity schema never leaks).
_FINDINGS_XML_RE = re.compile(
    r'<\s*(?:antml:)?parameter\s+name="findings"\s*>\s*(\[.*\])', re.DOTALL,
)
_FINDINGS_JSON_RE = re.compile(r'"findings"\s*:\s*(\[.*\])', re.DOTALL)


def _salvage_research_findings(disc: Optional["_ResearchDiscovery"]) -> Optional["_ResearchDiscovery"]:
    """Recover findings the model leaked into `answer` as a serialized array
    instead of the structured field. No-op when findings already populated or
    no leak is present. Trims the leaked blob off the answer so the UI shows
    clean prose. Fail-safe: any parse error leaves `disc` untouched."""
    if disc is None or disc.findings:
        return disc
    answer = disc.answer or ""
    m = _FINDINGS_XML_RE.search(answer) or _FINDINGS_JSON_RE.search(answer)
    if not m:
        return disc
    blob = m.group(1)
    # The regex is greedy to the last ']'; walk back to a json-parseable array.
    parsed = None
    while blob.endswith("]"):
        try:
            parsed = json.loads(blob)
            break
        except Exception:
            cut = blob.rfind("]", 0, len(blob) - 1)
            if cut == -1:
                break
            blob = blob[: cut + 1]
    if not isinstance(parsed, list) or not parsed:
        return disc
    recovered: List[_ResearchFinding] = []
    for item in parsed:
        if not isinstance(item, dict):
            continue
        try:
            recovered.append(_ResearchFinding(**{
                "title": item.get("title", ""),
                "kind": item.get("kind", "observation"),
                "severity": item.get("severity", "medium"),
                "detail": item.get("detail", ""),
                "evidence": item.get("evidence", []) or [],
                "suggested_action": item.get("suggested_action", "") or "",
                "graph_signal": item.get("graph_signal", "") or "",
            }))
        except Exception:
            continue
    if recovered:
        disc.findings = recovered
        # Strip the leaked blob + any stray open/close param tags off the prose.
        clean = answer[: m.start()]
        clean = re.sub(r'<\s*/?\s*(?:antml:)?parameter[^>]*>\s*$', "", clean).rstrip()
        clean = clean.rstrip("<").rstrip()
        disc.answer = clean
        logger.info("research: salvaged %d findings leaked into answer", len(recovered))
    return disc


# ─── Discovery — code-blob builder ───────────────────────────────────────────

# Total body budget for the discovery code blob, in CHARS (≈ chars/4 tokens).
# Design: this is MAP-FIRST. The cheap SYMBOL MAP (signatures of every parseable
# file) gives breadth — the model can NAME any file as relevant — so the body
# budget only needs to carry the top-scored files' code for discovery context,
# NOT every file in full. The critic gets verification DEPTH separately, from
# finding_critic.finding_aware_code_blob (full content of CITED files). So this
# budget is deliberately set BELOW the old worst case (8 files × 3500 = 28000)
# to actually reduce tokens, not grow them. With the symbol map (~3-5k chars)
# carrying breadth, a 14k body budget keeps the TOTAL blob at/below the old
# ~17k worst case — a net token reduction WITH the blindness fix, not a trade.
# Env-tunable for big monorepos.
_CONTEXT_BUDGET_CHARS = int(os.getenv("SHIPMATE_CONTEXT_BUDGET_CHARS", "14000"))

# Chars reserved for the elision marker inside a head+tail slice, so the sliced
# result never exceeds the caller's budget.
_ELISION_MARKER_CHARS = 50


def _head_tail(content: str, budget: int) -> str:
    """Slice a too-large file to AT MOST `budget` chars (marker included),
    keeping BOTH ends — the head (imports, top-level wiring) and the tail
    (late-defined gates/guards a head-only cut would hide). ~60% head / 40%
    tail. For a budget too small to slice meaningfully, return a plain head cut."""
    if len(content) <= budget:
        return content
    if budget <= _ELISION_MARKER_CHARS + 20:
        return content[:max(budget, 0)]
    body_budget = budget - _ELISION_MARKER_CHARS
    head = int(body_budget * 0.6)
    tail = body_budget - head
    omitted = len(content) - body_budget
    marker = f"\n\n# … [{omitted} chars elided — middle of file] …\n\n"
    return content[:head] + marker + (content[-tail:] if tail > 0 else "")


def _repo_code_blob(
    context: Dict[str, Any],
    max_files: int = 5,
    max_chars_per_file: int = 3500,  # retained for back-compat; superseded by budget
    prefer: tuple = (),
    budget_chars: Optional[int] = None,
) -> str:
    """
    Build a compact, MAP-FIRST text blob: a symbol skeleton of every parseable
    file (so no symbol is ever fully invisible), the file tree, then the bodies
    of the top-scored files filled to a token-aware budget (no blind per-file
    tail truncation).

    `prefer`: substrings that boost a file's relevance score. e.g. ("auth",
    "cors") for the GuardRail discovery pass. RepoLens entry_points are
    always given top priority. `budget_chars` overrides the global default for
    the total BODY budget (the symbol map + tree are cheap and not counted).
    """
    file_tree: List[str] = context.get("file_tree") or []
    key_files: Dict[str, str] = context.get("key_files") or {}
    repo_lens = context.get("repo_lens")
    entry_points = list(repo_lens.entry_points) if repo_lens else []
    budget = budget_chars if budget_chars is not None else _CONTEXT_BUDGET_CHARS

    # Score every key_file path by how relevant it looks.
    def _score(path: str) -> int:
        s = 0
        if path in entry_points:
            s += 100
        lp = path.lower()
        for token in prefer:
            if token in lp:
                s += 30
        # Penalize very common boilerplate.
        if any(skip in lp for skip in ("__pycache__", "node_modules", ".min.", "lock")):
            s -= 50
        # Slight boost to short-ish source files.
        if any(lp.endswith(ext) for ext in (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs")):
            s += 10
        return s

    # Filter to entries with content (key_files store both filename and full
    # path keys; prefer the path-keyed entries to avoid duplicates).
    candidates = [
        (path, content)
        for path, content in key_files.items()
        if content and "/" in path
    ]
    if not candidates:
        # Fallback to filename-keyed entries
        candidates = [(p, c) for p, c in key_files.items() if c]

    candidates.sort(key=lambda pc: _score(pc[0]), reverse=True)

    lines: List[str] = []

    # ── MAP FIRST — a symbol skeleton of EVERY parseable file we have. This is
    # the "find, don't chug" half: even a file whose body doesn't fit the budget
    # still has its importable symbols visible here, so the model can NAME it as
    # relevant instead of being blind to it. Reuses the same ast-export
    # extraction the Coder repo-map and the import lint use.
    try:
        from app.services.repo_map import _render_symbols
        symbol_map = _render_symbols(
            {p: c for p, c in candidates}, [p for p, _ in candidates],
        )
    except Exception:  # pragma: no cover - fail-open to no map
        symbol_map = ""
    if symbol_map:
        lines.append("## Symbol map — modules and their exported symbols")
        lines.append(symbol_map)
        lines.append("")

    if file_tree:
        # Top of the tree — first 60 paths is plenty of structural context.
        lines.append("## File tree (first 60 entries)")
        lines.extend(f"- {p}" for p in file_tree[:60])
        lines.append("")

    # ── BODIES — fill to a token-aware budget, scored order. A file shown is
    # shown WHOLE (no tail hidden); the single file that overflows the remaining
    # budget is head+tail-sliced; everything past the budget becomes a name-only
    # list so the model knows the file exists (and its symbols are in the map).
    # Note: discovery breadth comes from the symbol map; verification DEPTH for a
    # specific finding comes from the critic's finding_aware_code_blob (full
    # cited files), so a body that lands in the name list is not "blind" — its
    # symbols are mapped and the critic re-fetches it in full if it's cited.
    shown: List[tuple] = []
    overflow: List[str] = []
    spent = 0
    for idx, (path, content) in enumerate(candidates):
        remaining = budget - spent
        if len(shown) >= max_files or remaining <= 0:
            overflow.append(path)
            continue
        if len(content) <= remaining:
            shown.append((path, content))
            spent += len(content)
        else:
            # Doesn't fully fit — give it the rest of the budget head+tail-sliced
            # (keeps BOTH ends, so a deep tail gate stays visible), then stop
            # adding bodies. Use the loop index (not list.index, which would
            # ValueError on duplicate content) to push the rest to the name list.
            shown.append((path, _head_tail(content, remaining)))
            spent = budget
            overflow.extend(p for p, _ in candidates[idx + 1:])
            break

    if shown:
        lines.append(f"## Top {len(shown)} files (full content; budget-filled)")
        for path, content in shown:
            lines.append(f"\n### `{path}`")
            lines.append("```")
            lines.append(content)
            lines.append("```")

    if overflow:
        # De-dup while preserving order; cap the list so a huge repo can't bloat.
        seen: set = set()
        uniq = [p for p in overflow if not (p in seen or seen.add(p))]
        lines.append("")
        lines.append(
            f"## Other files present (not shown in full — see symbol map above): "
            + ", ".join(f"`{p}`" for p in uniq[:40])
            + (f" (+{len(uniq) - 40} more)" if len(uniq) > 40 else "")
        )

    return "\n".join(lines) if lines else "(no repo content available)"


# ─── Discovery — system prompts ──────────────────────────────────────────────

_DISCOVERY_PLAN_SYSTEM = (
    "You are a senior staff engineer reviewing a repo for shipping readiness. "
    "You have already been told what generic milestones a heuristic detector "
    "produced. Your job: read the ACTUAL CODE provided and INVENT concrete, "
    "repo-specific improvements the heuristic could not have found.\n\n"
    "AIM FOR A BALANCED MIX across these four buckets — do NOT only emit "
    "bugs. The heuristic already covers tests/CI/Docker/docs, so spend your "
    "5 slots on things heuristics cannot see:\n"
    "  • NEW FEATURES — capabilities the product is missing (new endpoints, "
    "    new pages, new agent passes, integrations, batch flows, etc.).\n"
    "  • EXISTING-FEATURE IMPROVEMENTS — UX/perf/quality tweaks to features "
    "    that already exist (streaming where it batches, persistence where "
    "    it's in-memory, caching, smarter defaults, retry/timeout policies).\n"
    "  • CODE-QUALITY TWEAKS — refactors that reduce duplication or risk "
    "    (extract a shared client, type-narrow returns, replace magic strings).\n"
    "  • BUGS — concrete defects in the actual code paths.\n\n"
    "Examples of GOOD discoveries (SHAPE only — judge against THIS repo's code "
    "AND the ALREADY-EXISTS lists in the user message; NEVER propose something "
    "those lists show is done):\n"
    "  [FEATURE]      'function/endpoint X in <file> has no batch variant — "
    "                  callers loop one-at-a-time; add a bulk path'\n"
    "  [IMPROVEMENT]  'handler Y in <file> retries on every error incl. 4xx — "
    "                  only retry 5xx/timeout to stop hammering a failing dep'\n"
    "  [TWEAK]        'two functions in <file> duplicate the same parse block — "
    "                  extract a shared helper'\n"
    "  [BUG]          'coro in <file> calls asyncio.gather without a timeout; "
    "                  one slow task hangs the whole request'\n\n"
    "Examples of BAD discoveries (DO NOT EMIT):\n"
    "  - ANYTHING in the ALREADY-EXISTS routes/modules lists given in the user "
    "message (history endpoints, SSE streaming, axios client, OAuth-state "
    "persistence, caching, provider factories, etc. may ALREADY be built — "
    "CHECK the lists first).\n"
    "  - anything in the DO-NOT-PROPOSE list (already shipped/dismissed/in-flight).\n"
    "  - 'add tests' / 'set up CI/CD' / 'write docs' / 'containerize' — already in heuristic base\n"
    "  - vague advice not tied to specific code\n"
    "  - duplicates of milestones already listed\n"
    "  - hypothetical future architecture not justified by current code\n\n"
    "Every milestone MUST cite at least one file path or code construct in "
    "its rationale. Prefer category='feature' for new capabilities, "
    "'infra' for product-improvement tweaks, 'docs' only for genuine "
    "developer-facing gaps. Emit blockers ONLY for issues that genuinely "
    "BLOCK shipping (broken paths, severe gaps); features and tweaks should "
    "be milestones, not blockers. Bias toward NOVELTY — fewer balanced, "
    "not-already-done items beat five obvious ones."
)

_DISCOVERY_GUARDRAIL_SYSTEM = (
    "You are a senior application-security engineer auditing a repo. A regex-"
    "based ruleset has already flagged obvious issues (hardcoded secrets, .env "
    "in git, wildcard CORS). Your job: read the auth, CORS, secrets-handling, "
    "and request-routing code and find risks the regex pass cannot see.\n\n"
    "Examples of GOOD discoveries:\n"
    "  - 'access_token is accepted as a query param; ends up in server logs and Referer headers'\n"
    "  - 'POST /api/analyze does not validate access_token before calling GitHub API'\n"
    "  - 'OAuth state is stored in-memory and lost on uvicorn --reload, weakening CSRF protection'\n"
    "  - 'PR creation endpoint trusts client-supplied owner/repo without authorization check'\n"
    "  - 'asyncio.to_thread call to BedrockProvider has no timeout; long requests pin a worker thread'\n\n"
    "Examples of BAD discoveries (DO NOT EMIT):\n"
    "  - duplicates of existing findings (compare against the base list before emitting)\n"
    "  - generic OWASP advice not grounded in this repo's code\n"
    "  - false positives — only emit if you can point to a specific construct\n\n"
    "Every finding MUST cite the file/function/line in its rationale. "
    "If the heuristic base already covers the area, skip it."
)

_DISCOVERY_TESTPILOT_SYSTEM = (
    "You are a senior QA engineer reviewing a repo for test coverage. The "
    "heuristic agent has already produced a generic template list "
    "(test_auth_flows, test_error_handling, test_e2e_happy_path) — those "
    "are USELESS for this repo because they don't cite any real function. "
    "Your job: read the ACTUAL CODE provided and propose tests that target "
    "specific functions, endpoints, edge cases, or branches.\n\n"
    "Examples of GOOD discoveries:\n"
    "  - name='test_analyze_with_invalid_repo_name_returns_400', "
    "    target_file='backend/app/api/routes/analysis.py', "
    "    rationale='analyze() asserts request.owner+repo but the 400 path "
    "    is not exercised; covers branches at routes/analysis.py:25-26'\n"
    "  - name='test_repo_analysis_service_skips_binary_files', "
    "    target_file='backend/app/services/repo_analysis_service.py', "
    "    rationale='_fetch_files swallows binary fetch failures silently; "
    "    needs a test that confirms binary content does not poison key_files'\n"
    "  - name='test_actuate_returns_502_on_github_pr_failure', "
    "    target_file='backend/app/api/routes/actuate.py', "
    "    rationale='actuate route maps httpx.HTTPStatusError to 502 — verify "
    "    the mapping and that the GitHub error body is preserved'\n"
    "  - name='test_planforge_dedup_by_normalized_title', "
    "    target_file='backend/app/services/llm_service.py', "
    "    rationale='_merge_plan_discovery dedups by _norm_title — verify "
    "    \"Set Up CI/CD\" and \"Set up CI CD\" map to same key'\n\n"
    "Examples of BAD discoveries (DO NOT EMIT):\n"
    "  - 'test_auth_flows' / 'test_error_handling' / 'test_e2e_happy_path' / "
    "    'test_smoke_all_routes' (already in heuristic template — these are "
    "    BANNED)\n"
    "  - 'test the API works' (vague, no target)\n"
    "  - tests for code that doesn't exist in this repo\n\n"
    "Every test MUST have a specific target_file + rationale citing a "
    "concrete function/branch. The test name MUST encode the case under "
    "test (test_<thing>_<condition>_<expected>). Skip generic happy-path "
    "tests entirely — those are heuristic territory."
)

_DISCOVERY_OPPORTUNITY_SYSTEM = (
    "You are a senior staff engineer and product-minded tech lead doing a "
    "'what should we build next' review of a repo. You are given the file tree "
    "and the contents of the most important files. Your job: propose concrete, "
    "high-VALUE self-improvement opportunities that a coding agent could then "
    "implement.\n\n"
    "Produce a BALANCED mix across these four categories — do NOT emit only "
    "bugs or only features:\n"
    "  • feature      — a genuinely new capability the product is missing "
    "(new endpoint, new page, new agent pass, integration, batch flow).\n"
    "  • improvement  — make an EXISTING feature better (streaming where it "
    "batches, persistence where it's in-memory, caching, retry/timeout, "
    "smarter defaults, better error UX).\n"
    "  • tweak        — a focused code-quality refactor that reduces real risk "
    "or duplication (extract a shared client, type-narrow, kill magic strings).\n"
    "  • bug          — a concrete defect in an actual code path you can point to.\n\n"
    "Every opportunity MUST:\n"
    "  1. Cite REAL files in `evidence` — paths that appear in the tree/code "
    "shown. An opportunity with no real evidence is worthless; do not emit it.\n"
    "  2. Name `target_files` that EXIST in the repo (the work would touch them).\n"
    "  3. Have a `rationale` that quotes a specific construct/function/gap.\n"
    "  4. Be IMPLEMENTABLE in <=21 days by one engineer — not a rewrite.\n\n"
    "Examples of GOOD opportunities (SHAPE only — judge against THIS repo's "
    "code and its ALREADY-EXISTS lists; never propose something the lists show "
    "is done):\n"
    "  [improvement] 'function X in <file> retries on every error including 4xx "
    "client errors — only retry on 5xx/timeout to avoid hammering a failing "
    "dependency.'\n"
    "  [bug]         'handler Y in <file> catches Exception and returns 200, "
    "masking real failures from the caller.'\n"
    "  [tweak]       'two functions in <file> duplicate the same 15-line parse "
    "block — extract a shared helper.'\n\n"
    "Examples of BAD opportunities (DO NOT EMIT):\n"
    "  - ANYTHING already present in the ALREADY-EXISTS routes/modules lists "
    "given in the user message (caching, history endpoints, provider factories, "
    "body sanitization, etc. may ALREADY be done — CHECK the lists first).\n"
    "  - 'add tests' / 'set up CI/CD' / 'write docs' / 'containerize' — generic.\n"
    "  - vague advice ('improve performance') with no file cited.\n"
    "  - speculative rewrites or future architecture not justified by the code.\n"
    "  - anything you cannot tie to a file in `evidence`.\n\n"
    "Bias HARD toward grounding, NOVELTY, and value. Five sharply-grounded, "
    "not-already-done opportunities beat ten obvious ones."
)


_DISCOVERY_INNOVATION_SYSTEM = (
    "You are a principal engineer + applied researcher running a BLUE-SKY "
    "innovation review of a repo. Unlike a conservative code review, your job "
    "is to propose AMBITIOUS, novel, high-ceiling ideas that would meaningfully "
    "advance what this product can do — the kind of idea a roadmap review would "
    "get excited about, not a lint fix.\n\n"
    "Lean into these shapes (mix them):\n"
    "  • feature      — a genuinely new CAPABILITY the product doesn't have: a "
    "new agent/analysis pass, a new modality, an integration, a feedback loop, "
    "a learning/eval mechanism, an autonomy upgrade.\n"
    "  • improvement  — a step-change to an existing capability (not a tweak): "
    "replace a heuristic with a learned signal, add a closed loop where it's "
    "open, make a single-shot pass iterative, add cross-run memory.\n"
    "  • research     — a direction worth prototyping even if the payoff is "
    "uncertain: a smarter retrieval/ranking strategy, an adversarial/verify "
    "stage, an A/B-able harness change. Mark these category='improvement' (the "
    "schema has no 'research' value) but be explicit in the description that "
    "it's exploratory.\n\n"
    "Crucial difference from a normal review: speculative, architectural, and "
    "future-facing ideas are WELCOME here — that's the point. But ambition is "
    "NOT an excuse for hand-waving. Every idea MUST still:\n"
    "  1. Cite REAL files in `evidence` — anchor the idea to code that exists "
    "(the system it extends, the seam it plugs into). An idea with no anchor is "
    "a daydream; do not emit it.\n"
    "  2. Name plausible `target_files` (existing files it would touch or sit "
    "beside) — new files are fine, but say which existing module they extend.\n"
    "  3. Explain in `rationale` WHY it's high-value AND why it's feasible to "
    "PROTOTYPE in <=21 days (a first cut, not the full vision).\n"
    "  4. Be NEW — do not propose anything in the ALREADY-EXISTS lists.\n\n"
    "GOOD innovation examples (SHAPE only — judge against THIS repo):\n"
    "  [feature]     'add a <new>_agent pass that does X, slotting into the "
    "orchestrator beside the existing agents in <file>.'\n"
    "  [improvement] 'the <pass> in <file> is single-shot + heuristic; add a "
    "verify→refine loop that re-scores its own output before emitting.'\n"
    "  [improvement] '(exploratory) replace the keyword scoring in <file> with "
    "a lightweight reference-graph signal to rank files by structural centrality.'\n\n"
    "BAD (DO NOT EMIT): generic 'add tests/CI/docs'; vague 'use AI/ML' with no "
    "seam named; anything in the ALREADY-EXISTS lists; ideas with no file anchor.\n\n"
    "Bias toward AMBITION + a real anchor. Four bold, anchored, not-already-done "
    "ideas beat ten safe ones."
)


_RESEARCH_SYSTEM = (
    "You are a staff engineer doing a DEEP CODEBASE RESEARCH pass. You are given "
    "the repo summary, the most important file bodies, and a REFERENCE-GRAPH "
    "SUMMARY computed from the real import/symbol edges (god modules by fan-in, "
    "import cycles, orphan modules, and unreferenced exports).\n\n"
    "Your job: answer the user's question (if one is asked) AND surface concrete "
    "findings — loops/holes/tweaks and messy DATAFLOW — that a maintainer should "
    "act on. Findings kinds:\n"
    "  • dataflow    — tangled/duplicated data paths, signature drift, a value "
    "threaded through many layers, redundant transforms.\n"
    "  • dead_code   — an unreferenced export / orphan module the graph flags "
    "(confirm it's truly unused, not an entry point or dynamically used).\n"
    "  • coupling    — a god-module the graph shows high fan-in to, or an import "
    "cycle; explain the concrete risk it creates.\n"
    "  • risk        — a latent bug / missing error path / unsafe assumption in "
    "an actual code path.\n"
    "  • observation — a noteworthy fact that isn't yet actionable.\n\n"
    "GROUNDING RULES (non-negotiable):\n"
    "  1. Every finding MUST cite REAL file paths in `evidence`.\n"
    "  2. When a finding is backed by the graph summary, put the exact signal in "
    "`graph_signal` (e.g. 'fan_in=11', 'cycle: a.py<->b.py', 'unreferenced "
    "export: parse_foo'). Prefer graph-supported findings — they're verifiable.\n"
    "  3. Do NOT invent edges the graph/code doesn't show. If the graph says a "
    "module is a god-module, trust it over a guess.\n"
    "  4. A dead_code finding the graph did NOT flag is suspect — say why you "
    "still believe it (e.g. 'defined but only referenced in a comment').\n\n"
    "Be specific and honest. A few sharp, graph-grounded findings beat a long "
    "list of vague ones."
)


# ─── Discovery — user prompt builders ────────────────────────────────────────

def _user_prompt_plan_discovery(
    context: Dict[str, Any], base: PlanForgeOutput, code_blob: str,
    *, capability_digest: str = "", exclude_titles: Optional[List[str]] = None,
) -> str:
    base_titles = [m.title for m in base.milestones]
    base_blocker_titles = [b.title for b in base.blockers]
    exclude_titles = exclude_titles or []
    digest_block = f"\n\n# {capability_digest}" if capability_digest else ""
    exclude_block = ""
    if exclude_titles:
        exclude_block = (
            "\n\n# ALREADY SHIPPED / DISMISSED / IN-FLIGHT milestones — DO NOT "
            "propose these again or anything overlapping them:\n"
            + "\n".join(f"- {t}" for t in exclude_titles[:60])
        )
    return (
        f"# Repo summary\n{_repo_summary(context)}"
        f"{digest_block}"
        f"{exclude_block}\n\n"
        f"# Heuristic milestones already covered (DO NOT duplicate)\n"
        + ("\n".join(f"- {t}" for t in base_titles) or "(none)")
        + f"\n\n# Heuristic blockers already covered (DO NOT duplicate)\n"
        + ("\n".join(f"- {t}" for t in base_blocker_titles) or "(none)")
        + f"\n\n# Repo code\n{code_blob[:18000]}\n\n"
        "Now emit up to 5 NEW milestones and up to 3 NEW blockers that the "
        "heuristic missed AND that are NOT in the ALREADY-EXISTS / DO-NOT-PROPOSE "
        "lists above. Each item MUST cite a specific file path or code construct "
        "in its rationale. Skip anything generic or already done. Return ONLY the "
        "PlanForgeDiscovery schema."
    )


def _user_prompt_guardrail_discovery(
    context: Dict[str, Any], base: GuardRailOutput, code_blob: str,
    *, capability_digest: str = "", exclude_titles: Optional[List[str]] = None,
) -> str:
    base_titles = [f.title for f in base.findings]
    exclude_titles = exclude_titles or []
    digest_block = f"\n\n# {capability_digest}" if capability_digest else ""
    exclude_block = ""
    if exclude_titles:
        exclude_block = (
            "\n\n# ALREADY SHIPPED / DISMISSED security findings — the control "
            "these ask for is ALREADY PRESENT. DO NOT propose them again or "
            "anything overlapping (e.g. don't re-flag 'token in query param' if "
            "a header/session auth dependency already exists):\n"
            + "\n".join(f"- {t}" for t in exclude_titles[:60])
        )
    return (
        f"# Repo summary\n{_repo_summary(context)}"
        f"{digest_block}"
        f"{exclude_block}\n\n"
        f"# Heuristic findings already covered (DO NOT duplicate)\n"
        + ("\n".join(f"- {t}" for t in base_titles) or "(none)")
        + f"\n\n# Repo code\n{code_blob[:18000]}\n\n"
        "Read the code carefully. Emit up to 5 NEW findings the regex pass "
        "missed AND that are NOT in the ALREADY-PRESENT / DO-NOT-PROPOSE lists "
        "above. A finding is only valid if the control it demands is genuinely "
        "ABSENT from the code shown — if the code already implements it (a "
        "session/header auth dependency, a sqlite-backed state store, a "
        "save_report call, a sanitiser, a security-headers middleware), DO NOT "
        "emit it. Cite the file + specific construct in each rationale. "
        "Return ONLY the GuardRailDiscovery schema."
    )


def _user_prompt_testpilot_discovery(
    context: Dict[str, Any], base: TestPilotOutput, code_blob: str,
) -> str:
    template_names = [t.name for t in base.suggested_tests]
    return (
        f"# Repo summary\n{_repo_summary(context)}\n\n"
        f"# Static template tests already emitted (DO NOT duplicate, DO NOT mention these names)\n"
        + ("\n".join(f"- {n}" for n in template_names) or "(none)")
        + f"\n\n# Generic missing-coverage areas already noted\n"
        + ("\n".join(f"- {a}" for a in base.missing_coverage_areas) or "(none)")
        + f"\n\n# Repo code\n{code_blob[:18000]}\n\n"
        "Now emit up to 5 NEW tests that target SPECIFIC functions, branches, "
        "or endpoints in the code above. Each test name MUST encode the case "
        "under test (e.g. test_<func>_<condition>_<expected>). Each MUST have "
        "a target_file and rationale citing a real construct in this repo. "
        "Also emit up to 3 missing_coverage_areas naming concrete code regions. "
        "Return ONLY the TestPilotDiscovery schema."
    )


def _capability_digest(context: Dict[str, Any]) -> str:
    """A deterministic 'what this repo ALREADY does' digest, built from the full
    corpus (every fetched file body + the tree). Two parts:
      • ROUTES — every HTTP route declared via FastAPI/Flask decorators or an
        axios/fetch call, so the model won't propose adding an endpoint that
        exists.
      • MODULES — notable service/agent/component file names, so the model won't
        propose creating a file/capability that's already present.
    This is the single biggest lever against recurrence: the model is stateless,
    so we MUST tell it what's done. Cheap regex, no LLM."""
    key_files: Dict[str, str] = context.get("key_files") or {}
    file_tree: List[str] = context.get("file_tree") or []

    routes: set = set()
    # FastAPI/Flask: @router.get("/path") / @app.post('/path')
    deco_re = re.compile(r"@\w+\.(?:get|post|put|patch|delete)\(\s*[\"']([^\"']+)[\"']", re.IGNORECASE)
    # Frontend: gh.get('/path') / axios.post("/path") / fetch(`${BASE}/path`)
    call_re = re.compile(r"\b(?:get|post|put|patch|delete)\(\s*[`\"']([^`\"']+)[`\"']", re.IGNORECASE)
    for body in key_files.values():
        if not body:
            continue
        for m in deco_re.finditer(body):
            routes.add(m.group(1))
        for m in call_re.finditer(body):
            p = m.group(1)
            if p.startswith("/") or "/api/" in p:
                routes.add(p)

    # Notable modules: service/agent/route/component file basenames from the tree.
    mod_tokens = ("/services/", "/agents/", "/routes/", "/orchestrator/",
                  "/components/", "/pages/", "/hooks/")
    modules: set = set()
    for p in file_tree:
        lp = p.lower()
        if any(t in lp for t in mod_tokens) and lp.endswith((".py", ".ts", ".tsx")):
            modules.add(p.split("/")[-1])

    route_list = sorted(r for r in routes if len(r) > 3)[:60]
    mod_list = sorted(modules)[:80]
    controls = _detect_security_controls(key_files, file_tree)
    parts = []
    if route_list:
        parts.append("## Routes/endpoints that ALREADY EXIST (do NOT propose adding these)\n"
                     + "\n".join(f"- {r}" for r in route_list))
    if mod_list:
        parts.append("## Service/agent/component modules that ALREADY EXIST "
                     "(do NOT propose creating these)\n"
                     + "\n".join(f"- {m}" for m in mod_list))
    if controls:
        parts.append("## Security controls ALREADY IMPLEMENTED — do NOT flag these "
                     "as missing (the proof is in the codebase, even if it's in a "
                     "file outside the snippet you were shown):\n"
                     + "\n".join(f"- {c}" for c in controls))
    return "\n\n".join(parts) if parts else "(no capability digest available)"


# (human-readable control statement, regex proving it exists in the full corpus).
# This is the generation-time fix for the GuardRail recurrence: the LLM only
# sees ~6 files in its discovery blob, so a control whose proof lives elsewhere
# (e.g. _consume_state in github_auth_service.py) looks "missing" to it. We scan
# the WHOLE corpus deterministically and TELL the model the control exists, so
# it never invents the finding — no matter how it would have phrased it.
_SECURITY_CONTROL_PROOFS = (
    ("OAuth `state` is validated server-side via a persistent (sqlite) single-use "
     "store — CSRF state survives restarts, NOT in-memory-only",
     re.compile(r"oauth_states|_consume_state|_store_state", re.IGNORECASE)),
    ("GitHub tokens are vaulted server-side (session_store): the client holds an "
     "opaque session id, never the raw token — no client-exposed token",
     re.compile(r"session_store|shipmate_sess_|def mint\(", re.IGNORECASE)),
    ("Auth credential is taken from the Authorization header only "
     "(resolve_access_token) — NOT from a URL query parameter",
     re.compile(r"resolve_access_token", re.IGNORECASE)),
    ("Repo write-access is verified before any mutating GitHub call "
     "(verify_repo_write_access)",
     re.compile(r"verify_repo_write_access", re.IGNORECASE)),
    ("Request bodies are sanitized for injection patterns by a middleware",
     re.compile(r"sanitize_input_middleware|_contains_dangerous_pattern", re.IGNORECASE)),
    ("CORS uses an explicit, validated origin allowlist (no wildcard with credentials)",
     re.compile(r"_validate_origin|_is_origin_allowed", re.IGNORECASE)),
    ("Security response headers (HSTS/X-Frame-Options/nosniff) are set by a middleware",
     re.compile(r"security_headers_middleware|x-frame-options|strict-transport-security", re.IGNORECASE)),
    ("Incoming GitHub webhooks are authenticated via HMAC signature verification",
     re.compile(r"_verify_github_webhook_signature|x-hub-signature|hmac\.compare_digest", re.IGNORECASE)),
    ("Analysis results are persisted to a sqlite store (report_store.save_report)",
     re.compile(r"save_report\(|report_store\.", re.IGNORECASE)),
)


def _detect_security_controls(key_files: Dict[str, str], file_tree: List[str]) -> List[str]:
    """Return plain-English statements for every security control whose proof is
    present anywhere in the full corpus. Fed to the discovery digest so GuardRail
    stops re-proposing already-implemented controls (the soft recurrence)."""
    blob_parts: List[str] = list(file_tree or [])
    blob_parts.extend(v for v in (key_files or {}).values() if v)
    blob = "\n".join(blob_parts)
    if not blob:
        return []
    return [statement for statement, proof in _SECURITY_CONTROL_PROOFS if proof.search(blob)]


def _journaled_titles(context: Dict[str, Any], namespaces: tuple) -> List[str]:
    """Titles already in the journal (dismissed / shipped / in_progress) for this
    repo, restricted to the given signature `namespaces` (e.g. ('milestone::',)
    or ('guardrail::', 'blocker::')). Fed to a discovery prompt as DO-NOT-PROPOSE
    so the stateless LLM stops re-inventing shipped work under new wording.
    Unlike filter_suppressed (which only HIDES dismissed/shipped from the visible
    list), this also excludes in_progress so a finding with an open PR isn't
    re-proposed mid-flight. Best-effort: [] on any error or missing repo."""
    repo_full = (context.get("repo_info") or {}).get("full_name", "") or ""
    if not repo_full:
        return []
    try:
        from app.services import inflight_registry as ir
        rows = ir.journal_list(repo_full_name=repo_full)
        titles: List[str] = []
        for r in rows:
            sig = str(r.get("finding_sig", ""))
            if not any(sig.startswith(ns) for ns in namespaces):
                continue
            parts = sig.split("::")
            if len(parts) >= 2 and parts[1]:
                titles.append(parts[1])
        return titles
    except Exception as e:  # pragma: no cover - defensive
        logger.debug("_journaled_titles failed (%s)", e)
        return []


def _journaled_milestone_titles(context: Dict[str, Any]) -> List[str]:
    """Milestone-namespace journaled titles (back-compat wrapper)."""
    return _journaled_titles(context, ("milestone::",))


def _user_prompt_opportunity_discovery(
    context: Dict[str, Any], code_blob: str, max_opportunities: int,
    *, capability_digest: str = "", exclude_titles: Optional[List[str]] = None,
) -> str:
    exclude_titles = exclude_titles or []
    exclude_block = ""
    if exclude_titles:
        exclude_block = (
            "\n\n# ALREADY PROPOSED / SHIPPED / DISMISSED — DO NOT propose any of "
            "these again, or anything that overlaps them:\n"
            + "\n".join(f"- {t}" for t in exclude_titles[:60])
        )
    digest_block = f"\n\n# {capability_digest}" if capability_digest else ""
    return (
        f"# Repo summary\n{_repo_summary(context)}"
        f"{digest_block}"
        f"{exclude_block}\n\n"
        f"# Repo code\n{code_blob[:20000]}\n\n"
        f"Propose up to {max_opportunities} self-improvement opportunities for "
        "THIS repo, balanced across feature / improvement / tweak / bug. HARD "
        "RULES:\n"
        "  • Do NOT propose anything in the ALREADY-EXISTS lists above — if a "
        "route or module is listed, that capability is DONE. Check before "
        "proposing.\n"
        "  • Do NOT propose anything overlapping the DO-NOT-PROPOSE list.\n"
        "  • Every opportunity MUST cite real file paths in `evidence` and name "
        "existing `target_files`.\n"
        "  • Skip anything generic or not tied to a specific file.\n"
        "Prefer DEEPER, less-obvious improvements (specific functions, edge "
        "cases, perf hotspots, missing error handling) over broad scaffolding. "
        "Return ONLY the OpportunityDiscovery schema."
    )


def _user_prompt_innovation_discovery(
    context: Dict[str, Any], code_blob: str, max_opportunities: int,
    *, capability_digest: str = "", exclude_titles: Optional[List[str]] = None,
) -> str:
    exclude_titles = exclude_titles or []
    exclude_block = ""
    if exclude_titles:
        exclude_block = (
            "\n\n# ALREADY PROPOSED / SHIPPED / DISMISSED — DO NOT propose any of "
            "these again, or anything that overlaps them:\n"
            + "\n".join(f"- {t}" for t in exclude_titles[:60])
        )
    digest_block = f"\n\n# {capability_digest}" if capability_digest else ""
    return (
        f"# Repo summary\n{_repo_summary(context)}"
        f"{digest_block}"
        f"{exclude_block}\n\n"
        f"# Repo code\n{code_blob[:20000]}\n\n"
        f"Propose up to {max_opportunities} AMBITIOUS, novel innovation ideas "
        "for THIS repo — new capabilities, step-change improvements, and "
        "exploratory research directions worth prototyping. HARD RULES:\n"
        "  • Do NOT propose anything in the ALREADY-EXISTS lists above, or "
        "overlapping the DO-NOT-PROPOSE list.\n"
        "  • Every idea MUST anchor to a REAL file in `evidence` (the system it "
        "extends / the seam it plugs into) — ambition is welcome, hand-waving "
        "is not.\n"
        "  • Say which existing module each idea extends in `target_files` "
        "(new files are fine alongside them).\n"
        "  • `rationale` must argue both VALUE and why a first cut is feasible "
        "in <=21 days.\n"
        "Favour bold, high-ceiling ideas over safe ones — but every one must be "
        "anchored in this repo's actual code. Return ONLY the OpportunityDiscovery "
        "schema."
    )


def _user_prompt_research(
    context: Dict[str, Any], code_blob: str, graph_summary: Dict[str, Any],
    *, question: str = "", max_findings: int = 8,
) -> str:
    graph_json = json.dumps(graph_summary or {}, default=list)[:8000]
    q_block = (
        f"# Question to answer\n{question.strip()}\n\n"
        if (question or "").strip()
        else "# No specific question — do an open dataflow/cleanup audit.\n\n"
    )
    return (
        f"# Repo summary\n{_repo_summary(context)}\n\n"
        f"{q_block}"
        f"# Reference-graph summary (computed from REAL import/symbol edges)\n"
        f"{graph_json}\n\n"
        f"# Repo code\n{code_blob[:20000]}\n\n"
        f"Answer the question (if any) and surface up to {max_findings} grounded "
        "findings (loops/holes/tweaks/dataflow). HARD RULES: every finding cites "
        "real files in `evidence`; when backed by the graph summary, put the exact "
        "signal in `graph_signal`; prefer graph-supported findings; do NOT invent "
        "edges. Return ONLY the _ResearchDiscovery schema."
    )


# ─── Discovery — merge / dedup ───────────────────────────────────────────────

def _norm_title(s: str) -> str:
    """Lowercase + strip non-alphanum for fuzzy dedupe."""
    return "".join(c for c in s.lower() if c.isalnum())


def _merge_plan_discovery(
    base: PlanForgeOutput, disc: PlanForgeDiscovery,
) -> PlanForgeOutput:
    base_titles = {_norm_title(m.title) for m in base.milestones}
    base_block_titles = {_norm_title(b.title) for b in base.blockers}

    new_milestones: List[Milestone] = []
    for d in disc.milestones[:5]:
        if _norm_title(d.title) in base_titles:
            continue  # dedup against heuristic
        try:
            new_milestones.append(Milestone(
                title=d.title.strip()[:120],
                description=d.description.strip(),
                estimated_days=max(1, min(21, d.estimated_days)),
                priority=d.priority.lower() if d.priority.lower() in
                    {"critical", "high", "medium", "low"} else "medium",
                category=d.category.lower() if d.category.lower() in
                    {"feature", "testing", "security", "ci_cd", "infra", "docs"} else "feature",
                source="discovery",
                rationale=d.rationale.strip()[:600],
            ))
        except Exception as e:
            logger.warning("Skipping malformed discovered milestone (%s)", e)

    next_bid = len(base.blockers) + 1
    new_blockers: List[Blocker] = []
    for d in disc.blockers[:3]:
        if _norm_title(d.title) in base_block_titles:
            continue
        try:
            new_blockers.append(Blocker(
                id=f"BLK-{next_bid:03d}",
                title=d.title.strip()[:120],
                description=d.description.strip(),
                severity=d.severity.lower() if d.severity.lower() in
                    {"critical", "high", "medium"} else "medium",
                resolution=d.resolution.strip(),
                category=d.category.strip().lower() or "structure",
                source="discovery",
                rationale=d.rationale.strip()[:600],
            ))
            next_bid += 1
        except Exception as e:
            logger.warning("Skipping malformed discovered blocker (%s)", e)

    if not new_milestones and not new_blockers:
        return base
    return base.model_copy(update={
        "milestones": list(base.milestones) + new_milestones,
        "blockers": list(base.blockers) + new_blockers,
    })


_VALID_SEVERITY = {"critical", "high", "medium", "low", "info"}
_VALID_GR_CATEGORY = {"secrets", "auth", "cors", "injection", "deps", "exposure", "config"}


def _merge_guardrail_discovery(
    base: GuardRailOutput, disc: GuardRailDiscovery,
) -> GuardRailOutput:
    base_keys = {(_norm_title(f.title), f.file or "") for f in base.findings}

    next_id = 1
    # Find next sec id by parsing existing ones
    for f in base.findings:
        if f.id.startswith("SEC-"):
            try:
                num = int(f.id.split("-")[1])
                next_id = max(next_id, num + 1)
            except (ValueError, IndexError):
                pass

    new_findings: List[SecurityFinding] = []
    for d in disc.findings[:5]:
        key = (_norm_title(d.title), d.file or "")
        if key in base_keys:
            continue
        sev_str = d.severity.lower()
        if sev_str not in _VALID_SEVERITY:
            sev_str = "medium"
        cat = d.category.lower()
        if cat not in _VALID_GR_CATEGORY:
            cat = "config"
        try:
            new_findings.append(SecurityFinding(
                id=f"SEC-{next_id:03d}",
                title=d.title.strip()[:120],
                severity=Severity(sev_str),
                category=cat,
                description=d.description.strip(),
                recommendation=d.recommendation.strip(),
                file=d.file,
                source="discovery",
                rationale=d.rationale.strip()[:600],
            ))
            next_id += 1
        except Exception as e:
            logger.warning("Skipping malformed discovered finding (%s)", e)

    if not new_findings:
        return base

    # Re-sort the combined list by severity so discovery findings interleave
    # naturally with heuristic ones.
    SEV_ORDER = {Severity.CRITICAL: 0, Severity.HIGH: 1, Severity.MEDIUM: 2,
                 Severity.LOW: 3, Severity.INFO: 4}
    combined = list(base.findings) + new_findings
    combined.sort(key=lambda f: SEV_ORDER.get(f.severity, 99))
    return base.model_copy(update={"findings": combined})


_VALID_TEST_TYPE = {"unit", "integration", "e2e", "security", "performance"}
_VALID_PRIORITY = {"critical", "high", "medium", "low"}

# Heuristic template names — block these from being re-emitted by the LLM
# even if our prompt warning fails. Substring match against discovery names.
_TEMPLATE_TEST_NAMES = {
    "test_smoke_all_routes",
    "test_auth_flows",
    "test_error_handling",
    "test_e2e_happy_path",
}


def _merge_testpilot_discovery(
    base: TestPilotOutput, disc: TestPilotDiscovery,
) -> TestPilotOutput:
    base_names = {_norm_title(t.name) for t in base.suggested_tests}

    new_tests: List[SuggestedTest] = []
    for d in disc.suggested_tests[:5]:
        norm = _norm_title(d.name)
        if norm in base_names:
            continue
        # Block obvious template-name leakage even though the prompt forbids it.
        if any(_norm_title(tpl) == norm for tpl in _TEMPLATE_TEST_NAMES):
            logger.info("TestPilot discovery: dropped template-shaped name %r", d.name)
            continue
        ttype = d.type.lower()
        if ttype not in _VALID_TEST_TYPE:
            ttype = "unit"
        prio = d.priority.lower()
        if prio not in _VALID_PRIORITY:
            prio = "medium"
        try:
            new_tests.append(SuggestedTest(
                name=d.name.strip()[:120],
                type=ttype,
                priority=prio,
                description=d.description.strip(),
                target_file=d.target_file,
                source="discovery",
                rationale=d.rationale.strip()[:600],
            ))
        except Exception as e:
            logger.warning("Skipping malformed discovered test (%s)", e)

    new_gaps: List[str] = []
    for area in (disc.missing_coverage_areas or [])[:3]:
        a = area.strip()
        if a and a not in base.missing_coverage_areas and a not in new_gaps:
            new_gaps.append(a[:200])

    if not new_tests and not new_gaps:
        return base
    return base.model_copy(update={
        "suggested_tests": list(base.suggested_tests) + new_tests,
        "missing_coverage_areas": list(base.missing_coverage_areas) + new_gaps,
    })


# ─── Opportunity — coerce / sanitize ─────────────────────────────────────────

_VALID_OPP_CATEGORY = {"feature", "improvement", "tweak", "bug"}
_VALID_OPP_EFFORT = {"S", "M", "L"}


def _coerce_opportunities(
    disc: "OpportunityDiscovery", max_opportunities: int,
) -> List[Opportunity]:
    """Turn the LLM discovery payload into validated Opportunity objects.
    Sanitizes enums, clamps numbers, assigns ids, dedups by normalized title.
    Ranking/grounding/journal-join happen later in OpportunityService — this
    only produces clean candidates. Malformed items are skipped, not fatal."""
    out: List[Opportunity] = []
    seen_titles: set = set()
    next_id = 1
    for d in (disc.opportunities or [])[: max_opportunities * 2]:  # room before dedup
        title = (d.title or "").strip()
        if not title:
            continue
        norm = _norm_title(title)
        if norm in seen_titles:
            continue
        cat = (d.category or "").strip().lower()
        if cat not in _VALID_OPP_CATEGORY:
            cat = "improvement"
        eff = (d.effort or "").strip().upper()
        if eff not in _VALID_OPP_EFFORT:
            eff = "M"
        try:
            out.append(Opportunity(
                id=f"OPP-{next_id:03d}",
                title=title[:120],
                category=cat,
                description=(d.description or "").strip(),
                impact=(d.impact or "").strip(),
                effort=eff,
                estimated_days=max(1, min(21, d.estimated_days)),
                target_files=[t.strip() for t in (d.target_files or []) if t.strip()][:4],
                suggested_approach=[s.strip() for s in (d.suggested_approach or []) if s.strip()][:4],
                evidence=[e.strip() for e in (d.evidence or []) if e.strip()][:3],
                rationale=(d.rationale or "").strip()[:600],
                source="discovery",
            ))
            seen_titles.add(norm)
            next_id += 1
        except Exception as e:
            logger.warning("Skipping malformed discovered opportunity (%s)", e)
        if len(out) >= max_opportunities:
            break
    return out
