"""ResearchService — codebase deep-research coordinator.

A deterministic sibling of OpportunityService: given an already-built repo
context (from RepoIndexService), it (1) builds the first-party reference graph
from the corpus, then (2) runs an LLM research pass GROUNDED on that graph to
answer a question and/or surface dataflow-cleanup findings (dead code, cycles,
god-modules, coupling, latent risks).

The graph is the grounding: the model is shown the real import/symbol edges, so
its "this module is over-coupled" / "this export is dead" claims are backed by
computed signals rather than guesses. Fail-open throughout: no provider ⇒ a
report with the graph summary + empty findings + ai_enhanced=False (the graph
itself is still useful), never an error.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict

from app.schemas.agent_schemas import ResearchFinding, ResearchReport
from app.services import reference_graph
from app.services.llm_service import LLMService

logger = logging.getLogger("shipmate.research_service")

_VALID_KINDS = {"dataflow", "dead_code", "coupling", "risk", "observation"}
_VALID_SEVERITY = {"high", "medium", "low"}


class ResearchService:
    """Stateless coordinator. Mirrors OpportunityService's shape."""

    @classmethod
    def research(
        cls,
        repo_context: Dict[str, Any],
        *,
        question: str = "",
        max_findings: int = 8,
    ) -> ResearchReport:
        info = repo_context.get("repo_info") or {}
        owner = (
            info.get("owner", {}).get("login", "")
            if isinstance(info.get("owner"), dict) else info.get("owner", "")
        )
        name = info.get("name", "")
        branch = repo_context.get("branch", "main")
        generated_at = datetime.now(timezone.utc).isoformat()

        # 1. Build the reference graph from the corpus (deterministic, no I/O).
        key_files = repo_context.get("key_files") or {}
        file_tree = repo_context.get("file_tree") or []
        try:
            graph = reference_graph.build_reference_graph(key_files, file_tree)
            graph_summary = graph.to_summary()
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("reference graph build failed (%s); empty graph", e)
            graph_summary = {}

        # 2. LLM research pass grounded on the graph (fail-open to graph-only).
        discovery = LLMService.research_codebase(
            repo_context, graph_summary, question=question, max_findings=max_findings,
        )
        ai_enhanced = LLMService.is_available() and discovery is not None

        findings = []
        if discovery is not None:
            for f in (discovery.findings or [])[:max_findings]:
                findings.append(ResearchFinding(
                    title=(f.title or "").strip()[:120],
                    kind=f.kind if f.kind in _VALID_KINDS else "observation",
                    severity=f.severity if f.severity in _VALID_SEVERITY else "medium",
                    detail=(f.detail or "").strip(),
                    evidence=list(f.evidence or [])[:3],
                    suggested_action=(f.suggested_action or "").strip(),
                    graph_signal=(f.graph_signal or "").strip(),
                ))

        return ResearchReport(
            owner=owner, repo=name, branch=branch,
            question=question or "",
            answer=(discovery.answer.strip() if discovery and discovery.answer else ""),
            findings=findings,
            graph_summary=graph_summary,
            ai_enhanced=ai_enhanced,
            generated_at=generated_at,
        )
