from datetime import datetime, timezone
from typing import Any, Dict, AsyncGenerator

from ..agents.repo_lens_agent import RepoLensAgent
from ..agents.plan_forge_agent import PlanForgeAgent
from ..agents.guardrail_agent import GuardRailAgent
from ..agents.testpilot_agent import TestPilotAgent
from ..services.scoring_service import ScoringService
from ..services.report_service import ReportService
from ..schemas.agent_schemas import (
    AgentOutputs, ShipMateReport, RepoInfo, ShipRecommendation
)


class ShipMateOrchestrator:
    """
    Runs the 4-agent pipeline and assembles the final ShipMateReport.

    Execution order:
      1. RepoLens  — repo structure, tech stack, architecture risks (context builder)
      2. PlanForge, GuardRail, TestPilot  — run with enriched context (parallel-safe)
      3. ScoringService  — deterministic weighted score
      4. ReportService   — final report assembly
    """

    def __init__(self):
        self.repo_lens = RepoLensAgent()
        self.plan_forge = PlanForgeAgent()
        self.guardrail = GuardRailAgent()
        self.testpilot = TestPilotAgent()

    def run(self, repo_context: Dict[str, Any]) -> ShipMateReport:
        """
        Run the full pipeline synchronously and return the assembled report.

        Canonical entry point used by POST /api/analyze and the autonomous loop
        (auto_fix). MUST stay sync-returning-ShipMateReport — callers do
        `report = orchestrator.run(ctx); report.model_dump()`. The incremental
        SSE variant is `run_stream` below; keep the two in sync.
        """
        repo_lens_out = self.repo_lens.run(repo_context)
        enriched = {**repo_context, "repo_lens": repo_lens_out}
        plan_forge_out = self.plan_forge.run(enriched)
        guardrail_out = self.guardrail.run(enriched)
        testpilot_out = self.testpilot.run(enriched)
        return self.assemble_report(
            repo_context, repo_lens_out, plan_forge_out, guardrail_out, testpilot_out,
        )

    async def run_stream(self, repo_context: Dict[str, Any]) -> AsyncGenerator[Dict[str, Any], None]:
        """
        Async generator yielding each agent's result incrementally, then a
        final assembled report. Backs the SSE endpoint POST /api/analyze/stream.

        Yields {"agent": <name>, "output": <result>} per agent, then
        {"agent": "report", "output": <ShipMateReport dict>} last.
        """
        # ── Step 1: RepoLens (must run first — other agents need its output) ──
        repo_lens_out = self.repo_lens.run(repo_context)
        yield {"agent": "repo_lens", "output": repo_lens_out}

        # Enrich context with RepoLens output
        enriched = {**repo_context, "repo_lens": repo_lens_out}

        # ── Step 2: Run remaining agents (all consume enriched context) ──
        plan_forge_out = self.plan_forge.run(enriched)
        yield {"agent": "plan_forge", "output": plan_forge_out}

        guardrail_out = self.guardrail.run(enriched)
        yield {"agent": "guardrail", "output": guardrail_out}

        testpilot_out = self.testpilot.run(enriched)
        yield {"agent": "testpilot", "output": testpilot_out}

        # ── Final: assembled report (score + full data) ──
        report = self.assemble_report(
            repo_context, repo_lens_out, plan_forge_out, guardrail_out, testpilot_out,
        )
        yield {"agent": "report", "output": report.model_dump()}

    def assemble_report(
        self,
        repo_context: Dict[str, Any],
        repo_lens_out: Any,
        plan_forge_out: Any,
        guardrail_out: Any,
        testpilot_out: Any,
    ) -> ShipMateReport:
        """
        Assembles the final ShipMateReport from all agent outputs.

        Args:
            repo_context: original repo context dict
            repo_lens_out: RepoLens agent output
            plan_forge_out: PlanForge agent output
            guardrail_out: GuardRail agent output
            testpilot_out: TestPilot agent output

        Returns:
            ShipMateReport
        """
        # ── Step 3: Score ──
        score_breakdown = ScoringService.calculate(
            repo_lens_out, plan_forge_out, guardrail_out, testpilot_out
        )
        final_score = score_breakdown["final_score"]
        recommendation = ScoringService.recommendation(final_score)

        # ── Step 4: Assemble report ──
        info = repo_context.get("repo_info", {})
        branch = repo_context.get("branch", "main")

        repo_info = RepoInfo(
            owner=info.get("owner", {}).get("login", "") if isinstance(info.get("owner"), dict) else info.get("owner", ""),
            name=info.get("name", ""),
            full_name=info.get("full_name", ""),
            branch=branch,
            description=info.get("description"),
            language=info.get("language"),
            stars=info.get("stargazers_count", 0),
            file_count=repo_lens_out.file_count,
            html_url=info.get("html_url", ""),
        )

        key_blockers = ReportService.extract_blockers(plan_forge_out, guardrail_out)
        next_actions = ReportService.extract_next_actions(plan_forge_out, guardrail_out, testpilot_out)

        return ShipMateReport(
            repo=repo_info,
            readiness_score=final_score,
            ship_recommendation=recommendation,
            score_breakdown=score_breakdown["breakdown"],
            agents=AgentOutputs(
                repo_lens=repo_lens_out,
                plan_forge=plan_forge_out,
                guardrail=guardrail_out,
                testpilot=testpilot_out,
            ),
            key_blockers=key_blockers,
            next_actions=next_actions,
            generated_at=datetime.now(timezone.utc).isoformat(),
        )
