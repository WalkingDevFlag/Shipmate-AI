const API_BASE_URL = import.meta.env.VITE_API_URL || 'http://localhost:8000';

export interface AnalysisRequest {
  repo_url: string;
  github_token?: string;
}

export interface AgentResult {
  agent: string;
  status: 'running' | 'complete' | 'error';
  result?: unknown;
  error?: string;
}

export interface AnalysisResponse {
  repo_url: string;
  agents: AgentResult[];
}

export async function analyzeRepo(request: AnalysisRequest): Promise<AnalysisResponse> {
  const response = await fetch(`${API_BASE_URL}/api/analyze`, {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
    },
    body: JSON.stringify(request),
  });

  if (!response.ok) {
    throw new Error(`Analysis failed: ${response.statusText}`);
  }

  return response.json();
}

export function streamAnalysis(
  request: AnalysisRequest,
  onEvent: (event: AgentResult) => void,
  onComplete: () => void,
  onError: (error: Error) => void
): () => void {
  const url = new URL(`${API_BASE_URL}/api/analyze/stream`);
  
  const controller = new AbortController();

  fetch(url.toString(), {
    method: 'POST',
    headers: {
      'Content-Type': 'application/json',
      'Accept': 'text/event-stream',
    },
    body: JSON.stringify(request),
    signal: controller.signal,
  })
    .then(async (response) => {
      if (!response.ok) {
        throw new Error(`Stream failed: ${response.statusText}`);
      }

      const reader = response.body?.getReader();
      if (!reader) {
        throw new Error('No response body');
      }

      const decoder = new TextDecoder();
      let buffer = '';

      while (true) {
        const { done, value } = await reader.read();
        if (done) break;

        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split('\n');
        buffer = lines.pop() || '';

        for (const line of lines) {
          if (line.startsWith('data: ')) {
            const data = line.slice(6).trim();
            if (data === '[DONE]') {
              onComplete();
              return;
            }
            try {
              const parsed: AgentResult = JSON.parse(data);
              onEvent(parsed);
            } catch {
              // skip malformed lines
            }
          }
        }
      }

      onComplete();
    })
    .catch((error: unknown) => {
      if (error instanceof Error && error.name !== 'AbortError') {
        onError(error);
      }
    });

  return () => controller.abort();
}
