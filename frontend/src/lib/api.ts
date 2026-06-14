import axios from 'axios';
import type {
  AnalyzeResponse, GitHubRepo, GitHubBranch, GitHubPR,
  ActuateResponse, FindingPayload, RepoLensSummary,
  WatcherState, WatcherLogLine, JournalResponse, AutoFixEvent, ResearchEvent,
  ShipMateReport, BuildPlanResponse, BuildExecuteResponse, Opportunity,
} from '../types';

// API base. Locally we default to '/api' and let the Vite dev proxy forward to
// the backend on :8000 (see vite.config.ts). When hosted there's no dev proxy,
// so the build injects VITE_API_BASE (e.g. https://shipmate-api.<region>
// .azurecontainerapps.io/api) at build time. Trailing slashes are trimmed so
// the per-call paths ('/auth/...') concatenate cleanly.
const BASE = (import.meta.env.VITE_API_BASE ?? '/api').replace(/\/$/, '');

const gh = axios.create({ baseURL: BASE });

// ── Auth token store + interceptor (OPP-001) ────────────────────────────────
// Previously every authenticated call passed the GitHub token as
// `?access_token=…`, which leaks it into server access logs, the Referer
// header, and browser history. We now hold the token in a module-level store
// and inject it as `Authorization: Bearer <token>` via a request interceptor —
// headers don't end up in any of those places. Call setAuthToken() once after
// the OAuth callback resolves (and on logout with null to clear it).
let _authToken: string | null = null;

export function setAuthToken(token: string | null): void {
  _authToken = token;
}

gh.interceptors.request.use((config) => {
  if (_authToken) {
    config.headers = config.headers ?? {};
    (config.headers as Record<string, string>).Authorization = `Bearer ${_authToken}`;
  }
  return config;
});

// Header builder for the raw fetch() SSE calls (which don't go through axios).
function authHeaders(extra: Record<string, string> = {}): Record<string, string> {
  return _authToken ? { ...extra, Authorization: `Bearer ${_authToken}` } : { ...extra };
}

export const api = {
  // ── Auth ────────────────────────────────────────────────────────────────

  async getAuthUrl(): Promise<string> {
    const { data } = await gh.get<{ auth_url: string }>('/auth/github/login');
    return data.auth_url;
  },

  async handleCallback(code: string, state: string) {
    const { data } = await gh.get('/auth/github/callback', { params: { code, state } });
    return data;
  },

  async getMe(token: string) {
    setAuthToken(token);
    const { data } = await gh.get('/auth/github/me');
    return data.user;
  },

  async logout(token: string) {
    setAuthToken(token);
    await gh.post('/auth/github/logout', null);
  },

  // ── Repos / Branches ────────────────────────────────────────────────────

  async getRepos(token: string): Promise<GitHubRepo[]> {
    setAuthToken(token);
    const { data } = await gh.get<{ repos: GitHubRepo[] }>('/auth/github/repos');
    return data.repos;
  },

  async getBranches(owner: string, repo: string, token: string): Promise<GitHubBranch[]> {
    setAuthToken(token);
    const { data } = await gh.get<{ branches: GitHubBranch[] }>(
      `/auth/github/repos/${owner}/${repo}/branches`,
    );
    return data.branches;
  },

  async getPulls(owner: string, repo: string, token: string): Promise<GitHubPR[]> {
    setAuthToken(token);
    const { data } = await gh.get<{ pulls: GitHubPR[] }>(
      `/auth/github/repos/${owner}/${repo}/pulls`,
    );
    return data.pulls;
  },

  // ── Analysis ────────────────────────────────────────────────────────────

  async analyze(params: {
    owner: string;
    repo: string;
    branch: string;
    access_token: string;
    pr_number?: number;
    feature_context?: string;
  }): Promise<AnalyzeResponse> {
    const { data } = await gh.post<AnalyzeResponse>('/analyze', params);
    return data;
  },

  // Streaming analyze (SSE): fires `onEvent` per agent as it completes, returns
  // the final ShipMateReport. Same body as analyze(); the backend emits
  // event:agent.done frames then event:report.done. Falls back to throwing on
  // a non-OK response so the caller can retry via the batch analyze().
  async streamAnalyze(
    params: {
      owner: string; repo: string; branch: string; access_token: string;
      pr_number?: number; feature_context?: string;
    },
    onEvent: (e: { event: string; agent?: string; output?: unknown; report?: unknown; detail?: string }) => void,
    signal?: AbortSignal,
  ): Promise<ShipMateReport | null> {
    const resp = await fetch(`${BASE}/analyze/stream`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(params),
      signal,
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`analyze stream failed: HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    let finalReport: ShipMateReport | null = null;
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        for (const line of frame.split('\n')) {
          if (line.startsWith('data: ')) {
            try {
              const evt = JSON.parse(line.slice(6));
              onEvent(evt);
              if (evt.event === 'report.done' && evt.report) {
                finalReport = evt.report as ShipMateReport;
              }
            } catch {
              // ignore malformed frame
            }
          }
        }
      }
    }
    return finalReport;
  },

  // ── Actuate (Coder → branch + PR) ───────────────────────────────────────

  async actuate(params: {
    owner: string;
    repo: string;
    branch: string;
    access_token: string;
    finding: FindingPayload;
    context?: RepoLensSummary;
    open_pr?: boolean;
  }): Promise<ActuateResponse> {
    const { data } = await gh.post<ActuateResponse>('/actuate', params, {
      timeout: 120_000,
    });
    return data;
  },

  // Streaming actuate (SSE, OPP-006): fires `onEvent` per pipeline phase
  // (resolve → coding → gate → branch → commit → pr → done). The terminal
  // `done` event carries { status, pr_url }. Same body as actuate(); falls
  // back to throwing on a non-OK response so callers can retry via actuate().
  async streamActuate(
    params: {
      owner: string; repo: string; branch: string; access_token: string;
      finding: FindingPayload; context?: RepoLensSummary; open_pr?: boolean;
    },
    onEvent: (e: { event: string; status?: string; pr_url?: string | null;
                   files?: string[]; paths?: string[]; phase?: string;
                   detail?: string }) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const resp = await fetch(`${BASE}/actuate/stream`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(params),
      signal,
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`actuate stream failed: HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        for (const line of frame.split('\n')) {
          if (line.startsWith('data: ')) {
            try {
              onEvent(JSON.parse(line.slice(6)));
            } catch {
              // ignore malformed frame
            }
          }
        }
      }
    }
  },

  // ── Build (Opportunity Planner) ───────────────────────────────────────────

  async buildPlan(params: {
    owner: string; repo: string; branch: string; access_token: string;
    max_opportunities?: number; include_ungrounded?: boolean;
    // "opportunity" (conservative fixes) | "innovation" (blue-sky ideas).
    mode?: 'opportunity' | 'innovation';
  }): Promise<BuildPlanResponse> {
    // Discovery + critic + ranker is a multi-LLM pass; allow generous time.
    const { data } = await gh.post<BuildPlanResponse>('/build/plan', params, {
      timeout: 180_000,
    });
    return data;
  },

  async buildExecute(params: {
    owner: string; repo: string; branch: string; access_token: string;
    opportunity: Opportunity; execute: boolean;
  }): Promise<BuildExecuteResponse> {
    // Plan + critique is quick; execute opens PRs (Coder per step) so allow long.
    const { data } = await gh.post<BuildExecuteResponse>('/build/execute', params, {
      timeout: 300_000,
    });
    return data;
  },

  async buildDismiss(params: {
    owner: string; repo: string; access_token: string; title: string; file?: string | null;
  }): Promise<{ signature: string; state: string }> {
    const { data } = await gh.post('/build/dismiss', params);
    return data;
  },

  // ── CI watcher ──────────────────────────────────────────────────────────

  async getWatcher(owner: string, repo: string, prNumber: number): Promise<WatcherState | null> {
    try {
      const { data } = await gh.get<WatcherState>(`/watcher/${owner}/${repo}/${prNumber}`);
      return data;
    } catch {
      // 404 = not watching this PR (yet). Treat as "no watcher".
      return null;
    }
  },

  async getWatcherLog(
    owner: string, repo: string, prNumber: number, sinceId = 0,
  ): Promise<{ lines: WatcherLogLine[]; next_id: number }> {
    const { data } = await gh.get<{ lines: WatcherLogLine[]; next_id: number }>(
      `/watcher/${owner}/${repo}/${prNumber}/log`,
      { params: { since_id: sinceId } },
    );
    return data;
  },

  // ── Finding journal ──────────────────────────────────────────────────────

  async getJournal(repoFullName: string): Promise<JournalResponse> {
    const { data } = await gh.get<JournalResponse>('/findings/journal', {
      params: { repo: repoFullName },
    });
    return data;
  },

  async dismissFinding(params: {
    kind: string; title: string; file?: string | null; repo_full_name: string; notes?: string;
  }): Promise<{ signature: string; state: string }> {
    const { data } = await gh.post('/findings/dismiss', params);
    return data;
  },

  async reopenFinding(params: {
    kind: string; title: string; file?: string | null; repo_full_name: string;
  }): Promise<{ signature: string; state: string }> {
    const { data } = await gh.post('/findings/reopen', params);
    return data;
  },

  // ── Auto-fix (server-side autonomous loop, SSE) ───────────────────────────
  //
  // EventSource can't send a POST body / Authorization, so we stream the
  // response with fetch + a ReadableStream reader and parse `data:` frames
  // ourselves. `onEvent` fires per parsed event; returns when the stream ends
  // or `signal` aborts.

  async startAutoFix(
    params: { owner: string; repo: string; branch: string; access_token: string; rounds?: number },
    onEvent: (e: AutoFixEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const resp = await fetch(`${BASE}/auto-fix/start`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(params),
      signal,
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`auto-fix stream failed: HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      // SSE frames are separated by a blank line.
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        for (const line of frame.split('\n')) {
          if (line.startsWith('data: ')) {
            try {
              onEvent(JSON.parse(line.slice(6)) as AutoFixEvent);
            } catch {
              // ignore malformed frame
            }
          }
        }
      }
    }
  },

  // ── Research harness (codebase deep-research / improve / innovate, SSE) ────
  // Same fetch+ReadableStream pattern as startAutoFix (EventSource can't POST a
  // body or send Authorization). `onEvent` fires per parsed `data:` frame.

  async startResearch(
    params: {
      owner: string; repo: string; branch: string; access_token: string;
      mode?: 'research' | 'improve' | 'innovate'; question?: string;
      max_findings?: number; max_opportunities?: number;
    },
    onEvent: (e: ResearchEvent) => void,
    signal?: AbortSignal,
  ): Promise<void> {
    const resp = await fetch(`${BASE}/research`, {
      method: 'POST',
      headers: authHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify(params),
      signal,
    });
    if (!resp.ok || !resp.body) {
      throw new Error(`research stream failed: HTTP ${resp.status}`);
    }
    const reader = resp.body.getReader();
    const decoder = new TextDecoder();
    let buffer = '';
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      let sep: number;
      while ((sep = buffer.indexOf('\n\n')) !== -1) {
        const frame = buffer.slice(0, sep);
        buffer = buffer.slice(sep + 2);
        for (const line of frame.split('\n')) {
          if (line.startsWith('data: ')) {
            try {
              onEvent(JSON.parse(line.slice(6)) as ResearchEvent);
            } catch {
              // ignore malformed frame
            }
          }
        }
      }
    }
  },
};
