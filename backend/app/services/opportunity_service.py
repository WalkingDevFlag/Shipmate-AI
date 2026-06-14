"""
OpportunityService — orchestrates the Phase-1A opportunity pipeline.

This is the thin coordinator the /api/build/plan route calls. It does NOT
re-fetch the repo or re-run the whole 4-agent pipeline — it reuses the same
context dict RepoAnalysisService already builds, runs RepoLens (cheap,
deterministic, needed for the code-blob's entry-point scoring), then:

    discover (LLM)  →  ground (deterministic)  →  suppress (journal)  →  rank

and returns a BuildPlanResponse. Plan-only: no Coder, no PRs, no journal writes
on the read path (selecting/building an opportunity is a separate Phase-1B/2
action that will mark it in_progress).

Everything is fail-open: an LLM outage yields an empty, ai_enhanced=False plan
rather than an error, exactly like the discovery passes elsewhere.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Dict, List

from app.agents.repo_lens_agent import RepoLensAgent
from app.schemas.agent_schemas import (
    BuildExecuteResponse, BuildPlanResponse, ExecutionPlan, Opportunity,
)
from app.services import opportunity_critic as critic
from app.services import plan_critic as pc
from app.services.llm_service import LLMService

logger = logging.getLogger("shipmate.opportunity_service")


class OpportunityService:
    """Stateless coordinator. Mirrors ShipMateOrchestrator's role but for the
    single-purpose opportunity pipeline."""

    _repo_lens = RepoLensAgent()

    @classmethod
    def build_plan(
        cls,
        repo_context: Dict[str, Any],
        *,
        max_opportunities: int = 8,
        include_ungrounded: bool = False,
        repo_lens: Any = None,
    ) -> BuildPlanResponse:
        """Run discover → ground → suppress → rank against an already-built
        repo context and return a ranked, grounded BuildPlanResponse.

        `repo_context` is the same dict RepoAnalysisService.build_context()
        produces (repo_info, file_tree, key_files, branch, …). `repo_lens`, if
        passed by the caller (e.g. the route already ran it via RepoIndexService),
        is reused instead of running RepoLens again — avoiding a duplicate pass."""
        info = repo_context.get("repo_info") or {}
        owner = (
            info.get("owner", {}).get("login", "")
            if isinstance(info.get("owner"), dict)
            else info.get("owner", "")
        )
        name = info.get("name", "")
        full_name = info.get("full_name", "") or (f"{owner}/{name}" if owner and name else "")
        branch = repo_context.get("branch", "main")
        generated_at = datetime.now(timezone.utc).isoformat()

        # RepoLens output enriches the code-blob's entry-point scoring (same as
        # the other discovery passes, which run after RepoLens in the pipeline).
        # Reuse a caller-supplied RepoLens (from RepoIndexService) when present;
        # otherwise run it here. This is what collapses the duplicate RepoLens
        # pass /build/* used to incur.
        if repo_lens is None:
            repo_lens = repo_context.get("repo_lens")
        try:
            if repo_lens is None:
                repo_lens = cls._repo_lens.run(repo_context)
            enriched = {**repo_context, "repo_lens": repo_lens}
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("RepoLens failed in opportunity pipeline (%s); proceeding without it", e)
            enriched = repo_context

        # 1. DISCOVER (LLM) — raw candidates, or [] if the provider is down.
        # Feed the journal's known titles (shipped/dismissed/in-flight) so the
        # stateless model stops re-proposing work that's already been surfaced.
        exclude = critic.journaled_titles(full_name)
        raw: List[Opportunity] = LLMService.discover_opportunities(
            enriched, max_opportunities=max_opportunities, exclude_titles=exclude,
        )
        ai_enhanced = LLMService.is_available()
        total_found = len(raw)

        if not raw:
            return BuildPlanResponse(
                owner=owner, repo=name, branch=branch,
                opportunities=[], total_found=0, grounded_count=0,
                ai_enhanced=ai_enhanced, generated_at=generated_at,
            )

        # 2. GROUND (deterministic) — verify cited evidence points at real files.
        file_tree = repo_context.get("file_tree") or []
        key_files = repo_context.get("key_files") or {}
        grounded = critic.ground_opportunities(raw, file_tree, key_files)
        grounded_count = sum(1 for o in grounded if getattr(o, "grounded", False))

        # 3a. ALREADY-BUILT prefilter (deterministic, FULL corpus) — the smoke
        # run showed the LLM critic can't refute "add X" when X's proof-of-
        # existence lives outside its ~8-file window. This scans every key_file
        # body + the tree for proposed routes/files that already exist and drops
        # them WITHOUT an LLM. Same lesson as finding_critic's prefilter.
        candidates = grounded if include_ungrounded else [o for o in grounded if getattr(o, "grounded", True)]
        candidates = critic.filter_already_built(candidates, file_tree, key_files)

        # 3b. VERIFY (Phase 1B — LLM critic) — catches the softer cases the
        # deterministic pass can't (duplicate intent, not-an-improvement),
        # judged against the SAME code blob the discoverer saw. Fail-open.
        provider = LLMService.provider()
        code_blob = LLMService.opportunity_code_blob(enriched) if provider else ""
        verified = critic.verify_opportunities(candidates, code_blob, provider)
        verified_sigs = {id(o) for o in verified}
        # Preserve any ungrounded items only when include_ungrounded (so the
        # debug view still shows them); otherwise the survivors are `verified`.
        if include_ungrounded:
            surviving = [o for o in grounded if (id(o) in verified_sigs or not getattr(o, "grounded", True))]
        else:
            surviving = verified
        verified_count = len(verified)

        # 4. SUPPRESS (journal) — drop dismissed/shipped (opportunity namespace).
        kept = critic.filter_suppressed(surviving, full_name)

        # 5. RANK (fold-in) — score, downrank in_progress, sort, derive priority.
        ranked = critic.rank_opportunities(
            kept, full_name, drop_ungrounded=not include_ungrounded
        )

        return BuildPlanResponse(
            owner=owner, repo=name, branch=branch,
            opportunities=ranked,
            total_found=total_found,
            grounded_count=grounded_count,
            verified_count=verified_count,
            ai_enhanced=ai_enhanced,
            generated_at=generated_at,
        )

    @classmethod
    def build_innovation_plan(
        cls,
        repo_context: Dict[str, Any],
        *,
        max_opportunities: int = 6,
        include_ungrounded: bool = False,
        repo_lens: Any = None,
    ) -> BuildPlanResponse:
        """Innovation sibling of `build_plan`: discover_innovations → ground →
        already-built prefilter → innovation_critic (feasibility, NOT suppress-
        novelty) → suppress (journal) → rank.

        Same context contract and same fail-open behaviour. The only differences
        from build_plan are the discovery posture (ambitious/novel) and the LLM
        verify posture (feasibility/coherence instead of worth-doing suppression);
        the deterministic floor (grounding + already-built) is identical, reused
        from opportunity_critic via innovation_critic."""
        from app.services import innovation_critic as icritic

        info = repo_context.get("repo_info") or {}
        owner = (
            info.get("owner", {}).get("login", "")
            if isinstance(info.get("owner"), dict) else info.get("owner", "")
        )
        name = info.get("name", "")
        full_name = info.get("full_name", "") or (f"{owner}/{name}" if owner and name else "")
        branch = repo_context.get("branch", "main")
        generated_at = datetime.now(timezone.utc).isoformat()

        if repo_lens is None:
            repo_lens = repo_context.get("repo_lens")
        try:
            if repo_lens is None:
                repo_lens = cls._repo_lens.run(repo_context)
            enriched = {**repo_context, "repo_lens": repo_lens}
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("RepoLens failed in innovation pipeline (%s); proceeding without it", e)
            enriched = repo_context

        # 1. DISCOVER (LLM, innovation posture).
        exclude = critic.journaled_titles(full_name)
        raw: List[Opportunity] = LLMService.discover_innovations(
            enriched, max_opportunities=max_opportunities, exclude_titles=exclude,
        )
        ai_enhanced = LLMService.is_available()
        total_found = len(raw)
        if not raw:
            return BuildPlanResponse(
                owner=owner, repo=name, branch=branch,
                opportunities=[], total_found=0, grounded_count=0,
                ai_enhanced=ai_enhanced, generated_at=generated_at,
            )

        # 2. GROUND + 3a. ALREADY-BUILT — the deterministic floor (shared).
        file_tree = repo_context.get("file_tree") or []
        key_files = repo_context.get("key_files") or {}
        grounded = critic.ground_opportunities(raw, file_tree, key_files)
        grounded_count = sum(1 for o in grounded if getattr(o, "grounded", False))
        candidates = grounded if include_ungrounded else [o for o in grounded if getattr(o, "grounded", True)]
        candidates = critic.filter_already_built(candidates, file_tree, key_files)

        # 3b. VERIFY (innovation posture — feasibility/coherence, keeps ambition).
        provider = LLMService.provider()
        code_blob = LLMService.opportunity_code_blob(enriched) if provider else ""
        verified = icritic.verify_innovations(candidates, code_blob, provider)
        verified_sigs = {id(o) for o in verified}
        if include_ungrounded:
            surviving = [o for o in grounded if (id(o) in verified_sigs or not getattr(o, "grounded", True))]
        else:
            surviving = verified
        verified_count = len(verified)

        # 4. SUPPRESS + 5. RANK — shared with the conservative pipeline.
        kept = critic.filter_suppressed(surviving, full_name)
        ranked = critic.rank_opportunities(
            kept, full_name, drop_ungrounded=not include_ungrounded
        )
        return BuildPlanResponse(
            owner=owner, repo=name, branch=branch,
            opportunities=ranked,
            total_found=total_found,
            grounded_count=grounded_count,
            verified_count=verified_count,
            ai_enhanced=ai_enhanced,
            generated_at=generated_at,
        )

    # ── Phase 1B — plan a chosen opportunity, critique it, optionally execute ──

    @classmethod
    async def execute_opportunity(
        cls,
        repo_context: Dict[str, Any],
        opportunity: Opportunity,
        access_token: str,
        *,
        execute: bool = False,
    ) -> BuildExecuteResponse:
        """Plan → critique → (optionally) actuate. The plan + critique are always
        produced (cheap, safe). Steps are only actuated when execute=True AND the
        critic approves. Each step runs through the existing CoderOrchestrator,
        so every step still passes lint/scope/pytest/resolution gates.

        DECOMPOSITION CONTRAST (intentional, two valid routes — see
        CoderOrchestrator._run_decomposed): this build/execute path runs each
        plan step as its OWN run_actuation with open_pr=True → N steps = N
        branches/PRs, stopping at the first failure so a broken step can't pile
        on a broken base. The actuate `decompose=True` flag (auto-fix UI) instead
        MERGES all steps into ONE CoderOutput → ONE branch/PR. Different by
        design: build/execute favours reviewable per-step PRs; the decompose flag
        favours a single atomic feature PR for one UI click."""
        import uuid
        from datetime import datetime, timezone
        from app.agents.planner_agent import PlannerAgent

        info = repo_context.get("repo_info") or {}
        owner = (
            info.get("owner", {}).get("login", "")
            if isinstance(info.get("owner"), dict) else info.get("owner", "")
        )
        name = info.get("name", "")
        branch = repo_context.get("branch", "main")
        file_tree = repo_context.get("file_tree") or []
        generated_at = datetime.now(timezone.utc).isoformat()
        ai_enhanced = LLMService.is_available()

        # Durable BuildRun: a UUID-keyed row tracks this run independent of the
        # HTTP request, so a crash/restart mid-build leaves an observable record
        # (status='actuating', steps_completed=N) instead of nothing. run_id is
        # surfaced on the response so the UI can poll /build/runs/{run_id}.
        full_name = info.get("full_name", "") or (f"{owner}/{name}" if owner and name else "")
        sig = critic.opportunity_signature(
            opportunity.title, (opportunity.target_files or [None])[0]
        )
        run_id = uuid.uuid4().hex
        cls._build_run_create(run_id, owner, name, branch, sig, opportunity.title)

        # 1. PLAN.
        plan: ExecutionPlan = PlannerAgent().plan(opportunity, file_tree)
        cls._build_run_update(run_id, status="critiquing", plan=plan,
                              step_count=len(plan.steps))

        # 2. CRITIQUE (deterministic + LLM, fail-open on the LLM axis).
        critique = pc.critique_plan(plan, opportunity, LLMService.provider())
        cls._build_run_update(run_id, critique=critique)

        base = BuildExecuteResponse(
            owner=owner, repo=name, branch=branch,
            opportunity_id=opportunity.id,
            plan=plan, critique=critique,
            executed=False, ai_enhanced=ai_enhanced, generated_at=generated_at,
        )
        base.run_id = run_id

        if not execute:
            base.status = "planned"
            base.summary = f"Plan ready ({len(plan.steps)} steps). Critic: {critique.reason}"
            # Plan-only preview is a terminal state for this run row.
            cls._build_run_update(run_id, status="done", result=base)
            return base

        if not critique.approved:
            base.status = "plan_rejected"
            base.summary = f"Plan rejected by critic, not executed: {critique.reason}"
            cls._build_run_update(run_id, status="plan_rejected", result=base)
            return base

        # Mark in_progress as we start actuating — so a concurrent/next plan run
        # downranks it (and, if we crash mid-build, it isn't re-proposed fresh).
        cls._journal(full_name, sig, "in_progress")
        cls._build_run_update(run_id, status="actuating")

        # 3. EXECUTE — actuate each step sequentially via CoderOrchestrator. Each
        # step is its own focused actuation (own branch/PR) so every gate applies
        # and a mid-plan failure leaves prior steps' PRs intact. Stops at the
        # first non-success so we don't pile bad steps on a broken base.
        from app.schemas.api_schemas import (
            ActuateRequest, FindingPayload, RepoLensSummary,
        )
        repo_lens = repo_context.get("repo_lens")
        ctx_summary = RepoLensSummary(
            primary_language=getattr(repo_lens, "primary_language", "Unknown") if repo_lens else "Unknown",
            tech_stack=list(getattr(repo_lens, "tech_stack", []) or []) if repo_lens else [],
            entry_points=list(getattr(repo_lens, "entry_points", []) or []) if repo_lens else [],
            has_ci_cd=getattr(repo_lens, "has_ci_cd", False) if repo_lens else False,
            has_tests=getattr(repo_lens, "has_tests", False) if repo_lens else False,
        )

        from app.services.coder_orchestrator import CoderOrchestrator
        pr_urls: List[str] = []
        files_changed: List[str] = []
        for step in plan.steps:
            finding = FindingPayload(
                kind=step.kind if step.kind in
                {"guardrail", "milestone", "blocker", "test", "next_action"} else "milestone",
                id=f"{opportunity.id}-S{step.index}",
                title=step.title,
                description=step.description,
                recommendation="; ".join(step.target_files),
                file=step.target_files[0] if step.target_files else None,
                category=opportunity.category,
            )
            actuate_req = ActuateRequest(
                owner=owner, repo=name, branch=branch, access_token=access_token,
                finding=finding, context=ctx_summary, open_pr=True,
            )
            try:
                resp = await CoderOrchestrator.run_actuation(actuate_req)
            except Exception as e:
                # Leave it in_progress (not shipped) — a transient failure should
                # be retryable, not marked done. Stays downranked next run.
                base.status = "execute_failed"
                base.executed = True
                base.pr_url = pr_urls[0] if pr_urls else None
                base.files_changed = files_changed
                base.summary = (
                    f"Executed {len(pr_urls)}/{len(plan.steps)} step(s); "
                    f"step {step.index} ('{step.title}') raised: {e}"
                )
                cls._build_run_update(
                    run_id, status="failed", result=base, pr_urls=pr_urls,
                    steps_completed=len(pr_urls), error=f"step {step.index}: {e}",
                )
                return base
            if resp.status != "complete":
                base.status = "execute_failed"
                base.executed = True
                base.pr_url = pr_urls[0] if pr_urls else (resp.pr_url or None)
                base.files_changed = files_changed
                base.summary = (
                    f"Executed {len(pr_urls)}/{len(plan.steps)} step(s); "
                    f"step {step.index} ('{step.title}') returned {resp.status}: {resp.summary[:200]}"
                )
                cls._build_run_update(
                    run_id, status="failed", result=base, pr_urls=pr_urls,
                    steps_completed=len(pr_urls),
                    error=f"step {step.index} returned {resp.status}",
                )
                return base
            if resp.pr_url:
                pr_urls.append(resp.pr_url)
            files_changed.extend(f.path for f in resp.files_changed)
            # Checkpoint after each landed step so a crash leaves an accurate
            # steps_completed/pr_urls trail on disk.
            cls._build_run_update(
                run_id, pr_urls=pr_urls, steps_completed=len(pr_urls),
            )

        # All steps landed — mark shipped so it's suppressed from future plans.
        cls._journal(full_name, sig, "shipped", pr_url=pr_urls[0] if pr_urls else None)
        base.status = "executed"
        base.executed = True
        base.pr_url = pr_urls[0] if pr_urls else None
        base.files_changed = files_changed
        base.summary = (
            f"Executed all {len(plan.steps)} step(s) — {len(pr_urls)} PR(s), "
            f"{len(files_changed)} file(s) changed."
        )
        cls._build_run_update(
            run_id, status="done", result=base, pr_urls=pr_urls,
            steps_completed=len(pr_urls),
        )
        return base

    # ── Journal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _journal(repo_full_name: str, sig: str, state: str, *, pr_url: str = None) -> None:
        """Best-effort journal write — never let a DB hiccup break the build flow."""
        if not repo_full_name or not sig:
            return
        try:
            from app.services import inflight_registry as ir
            ir.journal_set_state(sig, repo_full_name, state, pr_url=pr_url, bump_attempt=True)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("opportunity journal write failed (%s)", e)

    # ── BuildRun durability helpers (best-effort, never break the flow) ────────

    @staticmethod
    def _build_run_create(run_id, owner, repo, branch, sig, title) -> None:
        try:
            from app.services import inflight_registry as ir
            ir.create_build_run(run_id, owner, repo, branch, sig,
                                opportunity_title=title, status="planning")
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("build_run create failed (%s)", e)

    @staticmethod
    def _build_run_update(run_id, **fields) -> None:
        try:
            from app.services import inflight_registry as ir
            ir.update_build_run(run_id, **fields)
        except Exception as e:  # pragma: no cover - defensive
            logger.debug("build_run update failed (%s)", e)

    @classmethod
    def dismiss_opportunity(
        cls, repo_full_name: str, title: str, file: str = "",
    ) -> Dict[str, str]:
        """Mark an opportunity dismissed so it's suppressed from future plans AND
        excluded from discovery. Returns the signature + state."""
        sig = critic.opportunity_signature(title, file or None)
        cls._journal(repo_full_name, sig, "dismissed")
        return {"signature": sig, "state": "dismissed"}
