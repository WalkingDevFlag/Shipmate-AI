// ── GitHub auth ───────────────────────────────────────────────────────────────

export interface GitHubUser {
  id: number;
  login: string;
  name: string;
  avatar_url: string;
  html_url: string;
  email?: string;
  bio?: string;
  company?: string;
  location?: string;
  public_repos: number;
  followers: number;
  following: number;
}

export interface GitHubRepo {
  id: number;
  name: string;
  full_name: string;
  description: string | null;
  html_url: string;
  private: boolean;
  default_branch: string;
  language: string | null;
  stargazers_count: number;
  forks_count: number;
  updated_at: string | null;
  owner: { login: string; avatar_url: string };
}

export interface GitHubBranch {
  name: string;
  commit: { sha: string };
  protected: boolean;
}

export interface GitHubPR {
  number: number;
  title: string;
  state: string;
  html_url: string;
  user: { login: string };
  created_at: string;
}

// ── Agent outputs ─────────────────────────────────────────────────────────────

export type Severity = 'critical' | 'high' | 'medium' | 'low' | 'info';
export type ShipRecommendation =
  | 'ready_to_ship'
  | 'mostly_ready'
  | 'needs_review'
  | 'risky_release'
  | 'not_ready';

export interface ArchitectureRisk {
  risk: string;
  impact: string;
  category: string;
}

export interface RepoLensOutput {
  tech_stack: string[];
  primary_language: string;
  architecture_pattern: string;
  key_modules: string[];
  entry_points: string[];
  config_files: string[];
  has_ci_cd: boolean;
  has_dockerfile: boolean;
  has_tests: boolean;
  architecture_risks: ArchitectureRisk[];
  dependency_summary: Record<string, string[]>;
  file_count: number;
  repo_score: number;
}

export interface Milestone {
  title: string;
  description: string;
  estimated_days: number;
  priority: string;
  category: string;
  source?: 'heuristic' | 'discovery';
  rationale?: string | null;
}

export interface Blocker {
  id: string;
  title: string;
  description: string;
  severity: string;
  resolution: string;
  category: string;
  source?: 'heuristic' | 'discovery';
  rationale?: string | null;
}

export interface PlanForgeOutput {
  milestones: Milestone[];
  blockers: Blocker[];
  dependencies: string[];
  next_best_action: string;
  estimated_effort: string;
  delivery_score: number;
}

export interface SecurityFinding {
  id: string;
  title: string;
  severity: Severity;
  category: string;
  description: string;
  recommendation: string;
  file?: string;
  cve?: string;
  source?: 'heuristic' | 'discovery';
  rationale?: string | null;
}

export interface GuardRailOutput {
  findings: SecurityFinding[];
  exposed_secrets: string[];
  cors_issues: string[];
  auth_risks: string[];
  dependency_vulnerabilities: string[];
  security_score: number;
}

export interface ExistingTests {
  count: number;
  coverage_estimate: number;
  frameworks: string[];
  test_files: string[];
}

export interface SuggestedTest {
  name: string;
  type: string;
  priority: string;
  description: string;
  target_file?: string;
  source?: 'heuristic' | 'discovery';
  rationale?: string | null;
}

export interface TestPilotOutput {
  existing_tests: ExistingTests;
  missing_coverage_areas: string[];
  suggested_tests: SuggestedTest[];
  qa_readiness: string;
  test_score: number;
}

// ── Report ────────────────────────────────────────────────────────────────────

export interface ScoreBreakdown {
  repo_score: number;
  delivery_score: number;
  security_score: number;
  test_score: number;
}

export interface RepoInfo {
  owner: string;
  name: string;
  full_name: string;
  branch: string;
  description?: string;
  language?: string;
  stars: number;
  file_count: number;
  html_url: string;
}

export interface AgentOutputs {
  repo_lens: RepoLensOutput;
  plan_forge: PlanForgeOutput;
  guardrail: GuardRailOutput;
  testpilot: TestPilotOutput;
}

/** Optional PR-scoped risk summary. Populated only when the backend runs a
 *  PR-Risk pass (absent on this build), so it's always optional here — the PDF
 *  export reads it defensively and omits the section when undefined. */
export interface PRRiskOutput {
  pr_number: number;
  title: string;
  risk_level: string;   // "critical" | "high" | "medium" | "low"
  risk_score: number;
  summary: string;
  files_changed: number;
  additions: number;
  deletions: number;
}

export interface ShipMateReport {
  repo: RepoInfo;
  readiness_score: number;
  ship_recommendation: ShipRecommendation;
  score_breakdown: ScoreBreakdown;
  agents: AgentOutputs;
  key_blockers: string[];
  next_actions: string[];
  generated_at: string;
  /** False when the run degraded to pure heuristics (LLM provider
   *  unavailable) — the UI shows a heuristic-only banner. Optional for
   *  back-compat with older payloads. */
  ai_enhanced?: boolean;
  /** Present only for a PR-scoped analysis; used by the PDF export. */
  pr_risk?: PRRiskOutput | null;
}

export interface AnalyzeResponse {
  status: string;
  report: ShipMateReport;
}

// ── Actuate (Coder agent) ─────────────────────────────────────────────────────

export type FindingKind = 'guardrail' | 'milestone' | 'blocker' | 'test' | 'next_action';

export interface FindingPayload {
  kind: FindingKind;
  id: string;
  title: string;
  description: string;
  recommendation?: string;
  file?: string;
  severity?: string;
  category?: string;
}

export interface RepoLensSummary {
  primary_language?: string;
  tech_stack?: string[];
  entry_points?: string[];
  has_ci_cd?: boolean;
  has_tests?: boolean;
}

export interface ActuatedFile {
  path: string;
  rationale: string;
}

export interface ActuateResponse {
  status: string;
  pr_url?: string | null;
  branch_name: string;
  files_changed: ActuatedFile[];
  skipped: string[];
  summary: string;
}

// ── CI watcher (closed-loop CI feedback on a Coder-opened PR) ───────────────────

export interface WatcherState {
  owner: string;
  repo: string;
  pr_number: number;
  pr_url?: string | null;
  branch: string;
  status: 'watching' | 'fixing' | 'passed' | 'gave_up' | 'crashed';
  attempts: number;
  max_attempts: number;
  history: Array<{ attempt: number; files_changed: string[]; summary: string; patch_hash: string }>;
  last_error?: string | null;
  started_ago_s: number;
  last_event_ago_s: number;
  finding_id: string;
  finding_title: string;
}

export interface WatcherLogLine {
  id: number;
  ts: number;
  level: 'info' | 'warn' | 'error';
  msg: string;
}

// ── Finding journal ─────────────────────────────────────────────────────────────

export type JournalState = 'in_progress' | 'shipped' | 'dismissed' | 'parked';

export interface JournalRow {
  finding_sig: string;
  repo_full_name: string;
  state: JournalState;
  last_attempt_at: number;
  attempt_count: number;
  pr_url?: string | null;
  notes?: string | null;
}

export interface JournalResponse {
  repo: string;
  rows: JournalRow[];
  state_map: Record<string, JournalState>;
}

// ── Auto-fix SSE events ─────────────────────────────────────────────────────────

export interface AutoFixEvent {
  event:
    | 'loop.start' | 'analyze.start' | 'analyze.done' | 'finding.picked'
    | 'actuate.start' | 'actuate.done' | 'round.done' | 'loop.done'
    | 'error' | 'heartbeat';
  round?: number;
  rounds?: number;
  score?: number | null;
  picked?: number;
  kind?: string;
  title?: string;
  signature?: string;
  status?: string;
  pr_url?: string | null;
  files_changed?: (string | null)[];
  good?: number;
  skipped?: number;
  actuated?: number;
  note?: string;
  message?: string;
}

// ── Research harness (Research tab, SSE) ─────────────────────────────────────────

export interface ResearchEvent {
  event:
    | 'research.start' | 'index.done' | 'graph.done' | 'finding'
    | 'research.done' | 'propose.start' | 'opportunity' | 'propose.done'
    | 'done' | 'error';
  mode?: string;
  files?: number;
  // graph.done
  modules?: number;
  cycles?: number;
  god_modules?: number;
  orphans?: number;
  // finding
  title?: string;
  kind?: string;
  severity?: string;
  detail?: string;
  evidence?: string[];
  suggested_action?: string;
  graph_signal?: string;
  // research.done
  answer?: string;
  finding_count?: number;
  ai_enhanced?: boolean;
  // opportunity
  id?: string;
  category?: string;
  impact?: string;
  effort?: string;
  value_score?: number;
  priority?: string;
  target_files?: string[];
  rationale?: string;
  // done / propose.done
  findings?: number;
  opportunities?: number;
  count?: number;
  message?: string;
}

// ── Opportunity Planner (Build tab) ─────────────────────────────────────────────

export interface Opportunity {
  id: string;
  title: string;
  category: 'feature' | 'improvement' | 'tweak' | 'bug' | string;
  description: string;
  impact: string;
  effort: 'S' | 'M' | 'L' | string;
  estimated_days: number;
  target_files: string[];
  suggested_approach: string[];
  evidence: string[];
  rationale: string;
  value_score: number;
  priority: 'critical' | 'high' | 'medium' | 'low' | string;
  grounded: boolean;
  worth_doing: boolean;
  verify_reason?: string | null;
  journal_state?: string | null;
  source: string;
}

export interface BuildPlanResponse {
  owner: string;
  repo: string;
  branch: string;
  opportunities: Opportunity[];
  total_found: number;
  grounded_count: number;
  verified_count: number;
  ai_enhanced: boolean;
  generated_at: string;
}

export interface BuildStep {
  index: number;
  title: string;
  description: string;
  target_files: string[];
  depends_on: number[];
  kind: string;
  rationale: string;
}

export interface ExecutionPlan {
  opportunity_id: string;
  opportunity_title: string;
  summary: string;
  steps: BuildStep[];
  estimated_days: number;
  grounded: boolean;
  notes: string[];
}

export interface PlanCritique {
  approved: boolean;
  coherent: boolean;
  complete: boolean;
  in_scope: boolean;
  issues: string[];
  reason: string;
}

export interface BuildExecuteResponse {
  owner: string;
  repo: string;
  branch: string;
  opportunity_id: string;
  plan?: ExecutionPlan | null;
  critique?: PlanCritique | null;
  executed: boolean;
  pr_url?: string | null;
  files_changed: string[];
  status: 'planned' | 'plan_rejected' | 'executed' | 'execute_failed' | 'degraded' | string;
  summary: string;
  ai_enhanced: boolean;
  generated_at: string;
}

// ── App state ─────────────────────────────────────────────────────────────────

export type AppState =
  | 'not_connected'
  | 'connected'
  | 'analyzing'
  | 'complete'
  | 'error';

export type AgentStatus = 'idle' | 'running' | 'complete' | 'error';

export interface AgentProgress {
  id: 'repo_lens' | 'plan_forge' | 'guardrail' | 'testpilot';
  label: string;
  icon: string;
  description: string;
  status: AgentStatus;
}
