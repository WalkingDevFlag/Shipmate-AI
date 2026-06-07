export interface AnalysisReport {
  id: string;
  repoId: string;
  createdAt: string;
  summary: string;
  details: string;
  // ... other fields as needed
}

export interface RepoHistory {
  repoId: string;
  reports: AnalysisReport[];
}

export interface AgentProgress {
  step: string;
  status: string;
  percent: number;
}

export interface AnalysisHistoryResponse {
  repoId: string;
  history: AnalysisReport[];
}

export interface AnalysisError {
  message: string;
  code?: string;
}

export interface AnalysisContext {
  report: AnalysisReport | null;
  loading: boolean;
  error: string | null;
}

export type AnalysisHistory = AnalysisReport[];

export interface AnalysisHistoryHook {
  history: AnalysisHistory;
  loading: boolean;
  error: string | null;
}

export type SetState<T> = (value: T) => void;
