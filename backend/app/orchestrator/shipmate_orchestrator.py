import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, AsyncGenerator, Dict

from ..agents.repo_lens_agent import RepoLensAgent
from ..agents.plan_forge_agent import PlanForgeAgent
from ..agents.guardrail_agent import GuardRailAgent
from ..agents.testpilot_agent import TestPilotAgent
from ..services import run_trace
from ..services.scoring_service import ScoringService
from ..services.report_service import ReportService
from ..schemas.agent_schemas import (
    AgentOutputs, ShipMateReport, RepoInfo, ShipRecommendation
)

logger = logging.getLogger("shipmate.orchestrator")


class ShipMateOrchestrator:
    """
    Runs the 4-agent pipeline and assembles the final ShipMateReport.

    Execution order:
      1. RepoLens  — repo structure, tech stack, architecture risks (context builder)
      2. PlanForge, GuardRail, TestPilot  — run with enriched context (parallel-safe)
      3. ScoringService  — deterministic weighted score
      4. ReportService   — final report assembly
    """

    # ── Concurrency contract ────────────────────────────────────────────────
    # /analyze now offloads run() to a worker thread (asyncio.to_thread), so
    # multiple analyses can execute CONCURRENTLY. To keep that safe, construct
    # one orchestrator PER REQUEST (see new_per_request() / the route) rather
    # than sharing a module-global instance. The agents are currently stateless
    # (no self.* mutation outside __init__) and cheap to build, so per-request
    # construction is ~free and removes the latent risk that a future agent
    # caching something on self would bleed across concurrent requests.
    # INVARIANT: agents must stay stateless across run(); per-request scoping is
    # the backstop, not a license to add request state to a shared agent.

    def __init__(self):
        self.repo_lens = RepoLensAgent()
        self.plan_forge = PlanForgeAgent()
        self.guardrail = GuardRailAgent()
        self.testpilot = TestPilotAgent()

    @classmethod
    def new_per_request(cls) -> "ShipMateOrchestrator":
        """Factory for a fresh, request-scoped orchestrator with its own agent
        instances. Use this in request handlers instead of a shared global so
        concurrent analyses never share mutable agent state. Cheap — the agents
        do no I/O or heavy work in __init__."""
        return cls()

    def run(self, repo_context: Dict[str, Any]) -> ShipMateReport:
        """
        Args:
            repo_context: dict with keys:
                - repo_info: dict (from GitHub API)
                - file_tree: List[str]
                - key_files: Dict[str, str]
                - branch: str
                - feature_context: str (optional)
                - pr_info: dict (optional)
        Returns:
            ShipMateReport
        """
        # Establish a trace run so every nested LLM call (provider-level spans)
        # attributes to one run_id — the structured record that makes retry
        # waste / latency / errors queryable instead of log-archaeology.
        with run_trace.run(prefix="analyze") as run_id:
            # ── Step 1: RepoLens (must run first — other agents need its output) ──
            # Reuse a RepoLens output already attached to the context (e.g. by
            # RepoIndexService, which analyzes the repo once and shares the result)
            # instead of re-running the pass. Falls back to running it when absent.
            repo_lens_out = repo_context.get("repo_lens") or self.repo_lens.run(repo_context)

            # Enrich context with RepoLens output
            enriched = {**repo_context, "repo_lens": repo_lens_out}

            # ── Step 2: Run the 3 independent agents CONCURRENTLY ──
            # PlanForge / GuardRail / TestPilot each depend only on RepoLens, not
            # on each other, so they run in parallel on worker threads. This is
            # safe BECAUSE each request builds its own orchestrator (per-request
            # scope) and the agents are stateless — the documented invariant.
            plan_forge_out, guardrail_out, testpilot_out = self._run_independent_agents_sync(enriched)

            # ── Steps 3+4: Score + assemble (shared with run_stream) ──
            report = self._assemble_report(
                repo_context, repo_lens_out, plan_forge_out, guardrail_out, testpilot_out
            )
            try:
                logger.info("analyze run %s: %s", run_id, run_trace.run_summary(run_id))
            except Exception:
                pass
            return report

    def _run_independent_agents_sync(self, enriched: Dict[str, Any]):
        """Run PlanForge / GuardRail / TestPilot concurrently from a synchronous
        caller. Each agent's .run() is blocking (heuristics + LLM calls), so we
        fan them out onto threads and join. Falls back to sequential execution if
        no event loop machinery is available. Returns (plan, guardrail, testpilot)
        in fixed order regardless of completion order.

        Concurrency safety: the three agents share no mutable state (stateless
        invariant + per-request orchestrator), and they only READ `enriched`.
        Each LLM call is independently traced at the provider layer."""
        import concurrent.futures as _cf
        import contextvars

        agents = (
            ("plan_forge", self.plan_forge),
            ("guardrail", self.guardrail),
            ("testpilot", self.testpilot),
        )

        def _run_in_ctx(agent):
            # Each worker runs the agent inside a FRESH copy of the current
            # context so the active run_id (a ContextVar) propagates into the
            # thread — provider spans then attribute to this analyze run.
            return contextvars.copy_context().run(agent.run, enriched)

        results: Dict[str, Any] = {}
        with _cf.ThreadPoolExecutor(max_workers=3, thread_name_prefix="agent") as pool:
            futures = {
                pool.submit(_run_in_ctx, agent): name
                for name, agent in agents
            }
            for fut in _cf.as_completed(futures):
                name = futures[fut]
                results[name] = fut.result()
        return results["plan_forge"], results["guardrail"], results["testpilot"]

    async def run_stream(
        self, repo_context: Dict[str, Any]
    ) -> AsyncGenerator[Dict[str, Any], None]:
        """Async generator that yields one event per agent as it completes, then
        a final assembled report. Powers the SSE endpoint so the frontend renders
        each agent's result incrementally instead of waiting for the whole batch.

        Event shapes:
          {"event": "agent.done", "agent": "<name>", "output": {...}}
          {"event": "report.done", "report": {...}}
          {"event": "error", "detail": "..."}

        The agents' .run() methods are blocking (heuristics + one LLM call each),
        so each is dispatched via asyncio.to_thread to keep the event loop free
        while the SSE connection streams. The 3 independent agents (PlanForge /
        GuardRail / TestPilot) now run CONCURRENTLY — events are emitted in
        completion order, so the UI fills in whichever agent finishes first
        instead of waiting on a fixed sequential chain. run() stays the
        synchronous source of truth used by auto_fix and the non-streaming
        /analyze route."""
        # asyncio.to_thread propagates the current contextvars.Context into the
        # worker thread, so the run_id we set here reaches the provider spans.
        with run_trace.run(prefix="analyze-stream") as run_id:
            try:
                # Reuse a context-supplied RepoLens (RepoIndexService) if present.
                repo_lens_out = repo_context.get("repo_lens") or \
                    await asyncio.to_thread(self.repo_lens.run, repo_context)
                yield {"event": "agent.done", "agent": "repo_lens",
                       "output": repo_lens_out.model_dump()}

                enriched = {**repo_context, "repo_lens": repo_lens_out}

                # Fan the 3 independent agents out concurrently; yield each as it
                # completes (not in a fixed order). Each task carries its OWN name
                # back (no fragile result-type matching) so an agent can never be
                # silently lost, and the contextvar context propagates run_id into
                # the worker thread.
                async def _named(agent_name, fn):
                    return agent_name, await asyncio.to_thread(fn, enriched)

                tasks = [
                    asyncio.create_task(_named("plan_forge", self.plan_forge.run)),
                    asyncio.create_task(_named("guardrail", self.guardrail.run)),
                    asyncio.create_task(_named("testpilot", self.testpilot.run)),
                ]
                outputs: Dict[str, Any] = {}
                for coro in asyncio.as_completed(tasks):
                    name, done = await coro
                    outputs[name] = done
                    yield {"event": "agent.done", "agent": name,
                           "output": done.model_dump()}

                # Guard: every independent agent must have produced output before
                # we assemble. (as_completed surfaces any task exception above, so
                # a crashed agent already routed to the error frame; this catches
                # the should-never-happen partial case explicitly rather than
                # KeyError-ing.)
                missing = [k for k in ("plan_forge", "guardrail", "testpilot")
                           if k not in outputs]
                if missing:
                    raise RuntimeError(f"agent(s) produced no output: {missing}")

                report = self._assemble_report(
                    repo_context, repo_lens_out,
                    outputs["plan_forge"], outputs["guardrail"], outputs["testpilot"],
                )
                yield {"event": "report.done", "report": report.model_dump()}
            except Exception as e:  # pragma: no cover - defensive stream guard
                yield {"event": "error", "detail": f"Analysis failed: {e}"}

    def _assemble_report(
        self, repo_context, repo_lens_out, plan_forge_out, guardrail_out, testpilot_out
    ) -> ShipMateReport:
        """Score + assemble the final ShipMateReport from the four agent outputs.
        Shared by run() (sync) and run_stream() (SSE) so the assembly logic lives
        in exactly one place."""
        # Anti-recurrence: strip findings that are already-resolved in the repo
        # or dismissed/shipped in the journal BEFORE scoring + assembly. The
        # analyze pipeline had no equivalent of the Build path's already_built
        # gate, so it kept re-proposing shipped work (token-in-query after the
        # routes already use the header dep, "persist results" after save_report
        # is wired in) and keyword false-positives. Done here so both run() and
        # run_stream() get it, and so the SCORE reflects the filtered findings.
        plan_forge_out, guardrail_out, testpilot_out = self._filter_findings(
            repo_context, plan_forge_out, guardrail_out, testpilot_out
        )

        # Record what actually SURFACED (post-filter) as 'detected' in finding
        # memory — for yield metrics (detections vs shipped/dismissed). This does
        # NOT suppress future runs (detected ∉ suppressing states): an unresolved
        # finding must keep surfacing until shipped or dismissed. Fail-open.
        self._remember_detected(repo_context, guardrail_out, plan_forge_out)

        score_breakdown = ScoringService.calculate(
            repo_lens_out, plan_forge_out, guardrail_out, testpilot_out
        )
        final_score = score_breakdown["final_score"]
        recommendation = ScoringService.recommendation(final_score)

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

        # Was the LLM discovery/enhancement path live this run? If not, the
        # outputs are pure heuristics (generic milestones, template tests) and
        # the UI should say so rather than present them as AI findings.
        try:
            from app.services.llm_service import LLMService
            ai_enhanced = LLMService.is_available()
        except Exception:
            ai_enhanced = False

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
            ai_enhanced=ai_enhanced,
        )

    def _filter_findings(self, repo_context, plan_forge_out, guardrail_out, testpilot_out):
        """Apply the shared finding_critic gates to the analyze-side outputs so
        the diagnostic agents stop re-surfacing already-resolved / dismissed /
        false-positive findings — the same protection the Build path's
        already_built + journal gates give the Opportunity pipeline.

        Three passes per agent, all fail-open (any error keeps the findings):
          1. already-resolved prefilter — deterministic, scans the FULL corpus
             for proof the demanded control already exists.
          2. journal suppression — drop dismissed/shipped signatures.
          3. LLM critic verify (GuardRail only) — refute remaining false
             positives against the exact code blob.
        Returns the three (possibly-filtered) agent outputs."""
        try:
            from app.services import finding_critic as fc
        except Exception:
            return plan_forge_out, guardrail_out, testpilot_out

        file_tree = repo_context.get("file_tree") or []
        key_files = repo_context.get("key_files") or {}
        info = repo_context.get("repo_info") or {}
        owner = (
            info.get("owner", {}).get("login", "")
            if isinstance(info.get("owner"), dict) else info.get("owner", "")
        )
        full_name = info.get("full_name", "") or (
            f"{owner}/{info.get('name','')}" if owner and info.get("name") else ""
        )

        # GuardRail security findings — the noisiest surface.
        try:
            f = guardrail_out.findings
            f = fc.filter_already_resolved(f, file_tree, key_files, kind="guardrail")
            f = fc.filter_suppressed(f, "guardrail", full_name)
            # LLM critic verify against the real code blob (fail-open inside).
            # The critic used to judge against the TRUNCATED top-N blob (3500
            # chars/file), so a defense living deep in a big file (main.py's
            # proxy-trust gate at char ~6188) was invisible → false positives
            # recurred. Build a FINDING-AWARE blob that appends the FULL content
            # of each cited file, so the critic can actually see the control.
            try:
                from app.services.llm_service import LLMService
                provider = LLMService.provider()
                if provider:
                    base_blob = LLMService.opportunity_code_blob(repo_context)
                    code_blob = fc.finding_aware_code_blob(
                        f, key_files, fallback_blob=base_blob,
                    )
                    f = fc.verify_findings(f, code_blob, provider)
            except Exception:
                pass
            guardrail_out.findings = f
        except Exception as e:  # pragma: no cover - defensive
            pass

        # PlanForge blockers (milestones are roadmap items, not 'findings' — left
        # to the Build/Opportunity pipeline's own already_built gate).
        try:
            b = plan_forge_out.blockers
            b = fc.filter_already_resolved(b, file_tree, key_files, kind="blocker")
            b = fc.filter_suppressed(b, "blocker", full_name)
            plan_forge_out.blockers = b
        except Exception:
            pass

        # TestPilot suggested tests — suppress dismissed/shipped. SuggestedTest
        # uses `.name`, not `.title`, so adapt to the signature scheme.
        try:
            tests = testpilot_out.suggested_tests
            tests = fc.filter_suppressed_by_name(tests, "test", full_name)
            testpilot_out.suggested_tests = tests
        except Exception:
            pass

        return plan_forge_out, guardrail_out, testpilot_out

    def _remember_detected(self, repo_context, guardrail_out, plan_forge_out):
        """Record surfaced findings as 'detected' in finding_memory for yield
        metrics. Non-suppressing state + never downgrades a terminal row, so it
        can't hide a real finding. Fully fail-open — observability must never
        break the analyze path."""
        try:
            from app.services import finding_memory as fm
            from app.services.finding_critic import finding_signature
            info = repo_context.get("repo_info") or {}
            owner = (
                info.get("owner", {}).get("login", "")
                if isinstance(info.get("owner"), dict) else info.get("owner", "")
            )
            full_name = info.get("full_name", "") or (
                f"{owner}/{info.get('name','')}" if owner and info.get("name") else ""
            )
            if not full_name:
                return
            for kind, items in (
                ("guardrail", getattr(guardrail_out, "findings", []) or []),
                ("blocker", getattr(plan_forge_out, "blockers", []) or []),
            ):
                for f in items:
                    title = getattr(f, "title", "") or ""
                    if not title:
                        continue
                    sig = finding_signature(kind, title, getattr(f, "file", None))
                    fm.remember_detected(
                        full_name, sig, kind, title, getattr(f, "description", "") or "",
                    )
        except Exception as e:  # pragma: no cover - fail-open
            pass
