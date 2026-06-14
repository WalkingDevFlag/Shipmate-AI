"""innovation_critic — the verify posture for blue-sky innovation ideas.

The conservative opportunity pipeline runs `opportunity_critic.verify_opportunities`,
whose ONE job is to mark `worth_doing=FALSE` for anything speculative / not-yet-
proven (see its _VERIFY_SYSTEM: it suppresses "future architecture", duplicates,
and unimplemented capabilities). That posture is exactly WRONG for innovation
mode — it would kill every ambitious idea the innovation discovery prompt exists
to surface.

So innovation mode keeps the DETERMINISTIC FLOOR from opportunity_critic
(`ground_opportunities` — evidence must cite real files; `filter_already_built`
— don't propose what already exists) but swaps the LLM verify pass for one that
judges FEASIBILITY + COHERENCE + REAL-NOVELTY instead of suppressing speculation:

  • feasible_and_real=FALSE  → incoherent, self-contradictory, impossible to
    even prototype in the stated horizon, OR not actually novel (it's a rename
    of something the repo already does).
  • feasible_and_real=TRUE   → a bold idea anchored to a real seam in the code,
    prototypable as a first cut. Speculation is FINE here; that's the point.

Same fail-open contract as opportunity_critic: critic disabled / no provider /
no code / any exception ⇒ return the input unchanged (we would rather show a
borderline-ambitious idea than silently hide one).
"""
from __future__ import annotations

import logging
import os
from typing import Any, List

from pydantic import BaseModel, Field

# Reuse the deterministic floor verbatim — innovation still must be grounded and
# not-already-built; only the LLM *posture* differs.
from app.services.opportunity_critic import (  # noqa: F401  (re-exported for callers)
    filter_already_built,
    ground_opportunities,
    opportunity_signature,
    rank_opportunities,
)

logger = logging.getLogger("shipmate.innovation_critic")

# Separate env gate from the conservative critic so the two postures can be
# toggled independently.
_VERIFY_ENABLED = os.getenv("SHIPMATE_INNOVATION_VERIFY", "1").strip().lower() not in (
    "0", "false", "no",
)


class _InnovationVerdict(BaseModel):
    title: str = Field(..., description="The idea title being judged, verbatim.")
    feasible_and_real: bool = Field(
        ...,
        description=(
            "True if this is a coherent, prototypable, genuinely-novel idea "
            "anchored to the code. False ONLY if it is incoherent / self-"
            "contradictory, impossible to even prototype, or not actually novel "
            "(a rename of something the repo already does). Speculative is OK."
        ),
    )
    reason: str = Field(..., description="One sentence citing the seam it anchors to, or why it fails.")


class _InnovationCriticReport(BaseModel):
    verdicts: List[_InnovationVerdict] = Field(default_factory=list)


_VERIFY_SYSTEM = (
    "You are a pragmatic principal engineer triaging blue-sky INNOVATION ideas "
    "proposed for a codebase. These are SUPPOSED to be ambitious and "
    "speculative — your job is NOT to suppress speculation. Your job is to drop "
    "only the ideas that fail a low feasibility/coherence bar.\n\n"
    "Mark feasible_and_real=FALSE ONLY when one of these clearly holds:\n"
    "  1. INCOHERENT: the idea is self-contradictory, or its rationale "
    "references code/behaviour that plainly doesn't exist in what's shown.\n"
    "  2. NOT PROTOTYPABLE: even a first cut could not plausibly be built in the "
    "stated horizon (it's a multi-quarter rewrite masquerading as a feature).\n"
    "  3. NOT NOVEL: it's just a rename / restatement of something the code "
    "already does (the deterministic floor catches most of these, but you catch "
    "the soft ones).\n\n"
    "Mark feasible_and_real=TRUE for everything else — INCLUDING ambitious, "
    "architectural, or exploratory ideas, as long as they anchor to a real seam "
    "in the code and a first cut is plausible. When in doubt, default to TRUE "
    "(fail-open — a bold idea that needs scoping is more valuable than a "
    "silently-dropped one). Emit exactly one verdict per idea, echoing the "
    "title verbatim."
)


def verify_innovations(
    opportunities: List[Any],
    code_blob: str,
    provider: Any,
    deployment_hint: str = "smart",
) -> List[Any]:
    """LLM feasibility/coherence judge for innovation ideas. Drops only the
    incoherent / un-prototypable / not-novel; keeps the ambitious. Sets
    `.worth_doing` + `.verify_reason` on survivors (reusing the Opportunity
    fields the ranker/UI already read). Fail-open on every axis."""
    if not opportunities or not _VERIFY_ENABLED or provider is None or not code_blob:
        return opportunities
    try:
        listing = "\n".join(
            f"{i + 1}. [{getattr(o, 'category', '')}] {getattr(o, 'title', '')}: "
            f"{(getattr(o, 'description', '') or '')[:200]} "
            f"(anchor: {(getattr(o, 'evidence', None) or ['none'])[0]})"
            for i, o in enumerate(opportunities)
        )
        user = (
            "## Source code that was analyzed\n"
            f"{code_blob[:24000]}\n\n"
            "## Candidate innovation ideas to triage\n"
            f"{listing}\n\n"
            "For EACH idea decide feasible_and_real. Keep ambitious ideas that "
            "anchor to real code; drop only incoherent / un-prototypable / "
            "not-actually-novel ones. Echo each title verbatim."
        )
        report = provider.invoke_structured_sync(
            system_prompt=_VERIFY_SYSTEM,
            user_prompt=user,
            schema_class=_InnovationCriticReport,
            deployment_hint=deployment_hint,
        )
        refuted = {
            (v.title or "").strip().lower(): (v.reason or "")
            for v in report.verdicts
            if v.feasible_and_real is False
        }
        kept = []
        for o in opportunities:
            title_l = (getattr(o, "title", "") or "").strip().lower()
            if title_l in refuted:
                logger.info(
                    "innovation critic dropped (not feasible/novel): %s — %s",
                    getattr(o, "title", ""), refuted[title_l],
                )
                continue
            try:
                o.worth_doing = True
                o.verify_reason = "feasible + anchored (innovation triage)"
            except Exception:
                pass
            kept.append(o)
        return kept
    except Exception as e:  # fail-open: never hide ideas on a critic error
        logger.warning("innovation critic soft-failed (%s); keeping all", e)
        return opportunities
