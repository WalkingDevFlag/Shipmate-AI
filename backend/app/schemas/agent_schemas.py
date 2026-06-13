from pydantic import BaseModel
from typing import List, Optional, Dict
from enum import Enum


class Severity(str, Enum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"
    INFO = "info"


class ShipRecommendation(str, Enum):
    READY = "ready_to_ship"
    MOSTLY_READY = "mostly_ready"
    NEEDS_REVIEW = "needs_review"
    RISKY = "risky_release"
    NOT_READY = "not_ready"


# ── RepoLens ──────────────────────────────────────────────────────────────────

class ArchitectureRisk(BaseModel):
    risk: str
    impact: str   # "critical" | "high" | "medium" | "low"
    category: str  # "structure" | "deps" | "config" | "ci_cd" | "security" | "docs"
    evidence: Optional[str] = None   # file path or pattern that grounds this risk
    confidence: str = "high"         # "high" | "medium" | "low"


class RepoLensOutput(BaseModel):
    tech_stack: List[str]
    primary_language: str
    # "monolith" | "monorepo" | "microservices" | "fullstack" | "frontend" | "backend" | "library" | "unknown"
    architecture_pattern: str
    key_modules: List[str]
    entry_points: List[str]
    config_files: List[str]
    has_ci_cd: bool
    has_dockerfile: bool
    has_tests: bool
    architecture_risks: List[ArchitectureRisk]
    dependency_summary: Dict[str, List[str]]   # {"python": [...], "npm": [...]}
    file_count: int
    repo_score: int  # 0-100


# ── PlanForge ─────────────────────────────────────────────────────────────────

class Milestone(BaseModel):
    title: str
    description: str
    estimated_days: int
    priority: str   # "critical" | "high" | "medium" | "low"
    category: str   # "feature" | "testing" | "security" | "ci_cd" | "infra" | "docs"
    source: str = "heuristic"        # "heuristic" | "discovery"
    rationale: Optional[str] = None  # Discovery-only: why this matters for THIS repo


class Blocker(BaseModel):
    id: str
    title: str
    description: str
    severity: str   # "critical" | "high" | "medium"
    resolution: str
    category: str
    source: str = "heuristic"
    rationale: Optional[str] = None


class PlanForgeOutput(BaseModel):
    milestones: List[Milestone]
    blockers: List[Blocker]
    dependencies: List[str]
    next_best_action: str
    estimated_effort: str
    delivery_score: int  # 0-100


# ── GuardRail ─────────────────────────────────────────────────────────────────

class SecurityFinding(BaseModel):
    id: str
    title: str
    severity: Severity
    category: str   # "secrets" | "auth" | "cors" | "injection" | "deps" | "exposure" | "config"
    description: str
    recommendation: str
    file: Optional[str] = None
    cve: Optional[str] = None
    source: str = "heuristic"        # "heuristic" | "discovery"
    rationale: Optional[str] = None  # Discovery-only: code-grounded explanation


class GuardRailOutput(BaseModel):
    findings: List[SecurityFinding]
    exposed_secrets: List[str]
    cors_issues: List[str]
    auth_risks: List[str]
    dependency_vulnerabilities: List[str]
    security_score: int  # 0-100


# ── TestPilot ─────────────────────────────────────────────────────────────────

class ExistingTests(BaseModel):
    count: int
    coverage_estimate: int  # 0-100 percent
    frameworks: List[str]
    test_files: List[str]


class SuggestedTest(BaseModel):
    name: str
    type: str       # "unit" | "integration" | "e2e" | "security" | "performance"
    priority: str   # "critical" | "high" | "medium" | "low"
    description: str
    target_file: Optional[str] = None
    source: str = "heuristic"        # "heuristic" | "discovery"
    rationale: Optional[str] = None  # Discovery-only: code-grounded explanation


class TestPilotOutput(BaseModel):
    existing_tests: ExistingTests
    missing_coverage_areas: List[str]
    suggested_tests: List[SuggestedTest]
    qa_readiness: str   # "not_ready" | "partial" | "ready"
    test_score: int     # 0-100


# ── Aggregate ─────────────────────────────────────────────────────────────────

class AgentOutputs(BaseModel):
    repo_lens: RepoLensOutput
    plan_forge: PlanForgeOutput
    guardrail: GuardRailOutput
    testpilot: TestPilotOutput


class ScoreBreakdown(BaseModel):
    repo_score: int
    delivery_score: int
    security_score: int
    test_score: int


class RepoInfo(BaseModel):
    owner: str
    name: str
    full_name: str
    branch: str
    description: Optional[str] = None
    language: Optional[str] = None
    stars: int = 0
    file_count: int = 0
    html_url: str = ""


class ShipMateReport(BaseModel):
    repo: RepoInfo
    readiness_score: int
    ship_recommendation: ShipRecommendation
    score_breakdown: ScoreBreakdown
    agents: AgentOutputs
    key_blockers: List[str]
    next_actions: List[str]
    generated_at: str
    # True when the LLM discovery/enhancement path was live for this run; False
    # when it silently degraded to pure heuristics (expired creds / unreachable
    # provider). Lets the UI show a "heuristic-only" banner instead of leaving
    # the user wondering why findings look generic. Defaults True for back-compat
    # with any stored/older report that predates this field.
    ai_enhanced: bool = True


# ── Opportunity Planner (Phase 1A) ────────────────────────────────────────────
# An "opportunity" is a piece of self-improvement work ShipMate identifies for
# the repo: a new feature, an improvement to an existing one, a code-quality
# tweak, or a bug. It is the candidate input to the (Phase 1B) PlannerAgent and,
# later, the CoderOrchestrator. Phase 1A's job is ONLY to prove these are GOOD:
# grounded in real code, ranked by value, deduped against the journal — no
# planning or execution yet. The schema is intentionally "semi-plan-shaped"
# (target_files + suggested_approach) so the UI can show something actionable
# without committing to the full step-by-step plan that 1B will produce.

class Opportunity(BaseModel):
    id: str                                    # "OPP-001"
    title: str
    category: str                              # "feature" | "improvement" | "tweak" | "bug"
    description: str
    impact: str                                # what concretely gets better if shipped
    effort: str                                # "S" | "M" | "L" (rough t-shirt size)
    estimated_days: int                        # 1-21, mirrors Milestone
    target_files: List[str] = []               # semi-plan: files this would touch
    suggested_approach: List[str] = []         # semi-plan: 2-4 high-level steps (NOT a full plan)
    evidence: List[str] = []                   # grounding: file paths / constructs cited from THIS repo
    rationale: str = ""                        # WHY it matters here, citing real code
    value_score: int = 0                       # ranker output 0-100 (higher = ship sooner)
    priority: str = "medium"                   # derived from value_score: critical|high|medium|low
    grounded: bool = True                      # deterministic: evidence verified against the repo
    worth_doing: bool = True                   # 1B LLM critic: not already-built / duplicate / no-op
    verify_reason: Optional[str] = None        # 1B critic's one-line justification (when refuted upstream)
    journal_state: Optional[str] = None        # in_progress|shipped|dismissed|parked|None(fresh)
    source: str = "discovery"


class BuildPlanResponse(BaseModel):
    """Response of POST /api/build/plan — a ranked, grounded list of
    self-improvement opportunities for the repo. Plan-only: no PRs, no Coder."""
    owner: str
    repo: str
    branch: str = "main"
    opportunities: List[Opportunity] = []
    total_found: int = 0                       # before suppression/ranking truncation
    grounded_count: int = 0                    # how many passed deterministic grounding
    verified_count: int = 0                    # how many survived the 1B LLM critic (worth_doing)
    ai_enhanced: bool = True                   # False ⇒ LLM unavailable, list is empty/degraded
    generated_at: str = ""


# ── Phase 1B — Planner (opportunity → ordered, executable steps) ──────────────
# A BuildStep is one focused unit of work a Coder can actuate. An ExecutionPlan
# is the ordered, dependency-aware decomposition of a single Opportunity into
# such steps. PlanCritique is the PlanCritic's verdict on whether that plan is
# coherent, complete, and in-scope before any code is written.

class BuildStep(BaseModel):
    index: int                                 # 1-based order
    title: str
    description: str                           # what this step does, concretely
    target_files: List[str] = []              # existing/new files the step touches
    depends_on: List[int] = []                # indices of steps that must land first
    kind: str = "milestone"                    # maps to a CoderOrchestrator finding kind
    rationale: str = ""                        # why this step, citing code


class ExecutionPlan(BaseModel):
    opportunity_id: str
    opportunity_title: str
    summary: str                               # one-paragraph plan overview
    steps: List[BuildStep] = []
    estimated_days: int = 0                    # rolled up from the source opportunity
    grounded: bool = True                      # all step target_files exist or are plausible new files
    notes: List[str] = []                      # planner caveats / assumptions


class PlanCritique(BaseModel):
    """PlanCritic verdict on an ExecutionPlan."""
    approved: bool = True
    coherent: bool = True                      # steps form a sensible ordered whole
    complete: bool = True                      # nothing obviously missing to ship the opportunity
    in_scope: bool = True                      # no scope creep beyond the opportunity
    issues: List[str] = []                     # specific problems (empty ⇒ clean)
    reason: str = ""                           # one-line summary verdict


class BuildExecuteResponse(BaseModel):
    """Response of POST /api/build/execute — plan a chosen opportunity, critique
    the plan, and (when execute=true) actuate each step into ONE branch/PR."""
    owner: str
    repo: str
    branch: str = "main"
    opportunity_id: str = ""
    plan: Optional[ExecutionPlan] = None
    critique: Optional[PlanCritique] = None
    executed: bool = False                     # whether steps were actuated (vs plan-only)
    pr_url: Optional[str] = None
    files_changed: List[str] = []
    status: str = "planned"                    # planned|plan_rejected|executed|execute_failed|degraded
    summary: str = ""
    ai_enhanced: bool = True
    generated_at: str = ""
    # Durable BuildRun id — poll GET /api/build/runs/{run_id} for live/terminal
    # state independent of this request (a crash mid-build leaves the record).
    run_id: Optional[str] = None


class ResearchFinding(BaseModel):
    """One grounded codebase-research finding — a loop/hole/tweak/dataflow issue
    the research pass surfaced, anchored to a real file (and, where relevant, a
    reference-graph signal)."""
    title: str
    kind: str = "observation"                  # dataflow | dead_code | coupling | risk | observation
    severity: str = "medium"                   # high | medium | low
    detail: str                                # what it is, concretely, for THIS repo
    evidence: List[str] = []                   # real file paths / constructs that prove it
    suggested_action: str = ""                 # one concrete next step (not a full plan)
    graph_signal: str = ""                     # which graph signal backs it (e.g. "fan_in=11", "cycle", "unreferenced export")


class ResearchReport(BaseModel):
    """Response of POST /api/research — answers a codebase question and/or
    surfaces dataflow-cleanup targets, grounded on the reference graph."""
    owner: str
    repo: str
    branch: str = "main"
    question: str = ""                         # the asked question ("" = open audit)
    answer: str = ""                           # narrative answer (when a question was asked)
    findings: List[ResearchFinding] = []
    graph_summary: dict = {}                   # reference_graph.to_summary() — the grounding
    ai_enhanced: bool = True
    generated_at: str = ""
