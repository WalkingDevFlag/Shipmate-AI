import { useState, useEffect, useRef } from 'react';
import { useLocation, useNavigate } from 'react-router-dom';
import { streamAnalysis, AgentResult } from '../lib/api';

interface LocationState {
  repoUrl: string;
  githubToken?: string;
}

export default function AnalysisPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const state = location.state as LocationState | null;

  const [events, setEvents] = useState<AgentResult[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [done, setDone] = useState(false);
  const stopRef = useRef<(() => void) | null>(null);

  useEffect(() => {
    if (!state?.repoUrl) {
      navigate('/');
      return;
    }

    setStreaming(true);
    setError(null);
    setDone(false);
    setEvents([]);

    const stop = streamAnalysis(
      { repo_url: state.repoUrl, github_token: state.githubToken },
      (event) => {
        setEvents((prev) => {
          const idx = prev.findIndex((e) => e.agent === event.agent);
          if (idx >= 0) {
            const next = [...prev];
            next[idx] = event;
            return next;
          }
          return [...prev, event];
        });
      },
      () => {
        setStreaming(false);
        setDone(true);
      },
      (err) => {
        setStreaming(false);
        setError(err.message);
      }
    );

    stopRef.current = stop;

    return () => {
      stopRef.current?.();
    };
  }, [state, navigate]);

  const agents = ['RepoLens', 'PlanForge', 'TestPilot', 'GuardRail'];

  return (
    <div className="min-h-screen bg-gray-950 text-white p-8">
      <div className="max-w-4xl mx-auto">
        <div className="flex items-center justify-between mb-8">
          <h1 className="text-3xl font-bold">Analysis</h1>
          <button
            onClick={() => navigate('/')}
            className="text-gray-400 hover:text-white transition-colors"
          >
            ← Back
          </button>
        </div>

        {state?.repoUrl && (
          <p className="text-gray-400 mb-8 font-mono text-sm">{state.repoUrl}</p>
        )}

        {error && (
          <div className="bg-red-900/50 border border-red-500 rounded-lg p-4 mb-6">
            <p className="text-red-300">{error}</p>
          </div>
        )}

        <div className="grid gap-4">
          {agents.map((agentName) => {
            const result = events.find((e) => e.agent === agentName);
            return (
              <AgentCard
                key={agentName}
                name={agentName}
                result={result}
                streaming={streaming}
              />
            );
          })}
        </div>

        {done && (
          <div className="mt-8 text-center text-green-400 font-semibold">
            ✓ Analysis complete
          </div>
        )}
      </div>
    </div>
  );
}

interface AgentCardProps {
  name: string;
  result?: AgentResult;
  streaming: boolean;
}

function AgentCard({ name, result, streaming }: AgentCardProps) {
  const statusColor = !result
    ? 'border-gray-700'
    : result.status === 'complete'
    ? 'border-green-500'
    : result.status === 'error'
    ? 'border-red-500'
    : 'border-yellow-500';

  const statusLabel = !result
    ? streaming
      ? 'Waiting…'
      : 'Pending'
    : result.status === 'complete'
    ? 'Complete'
    : result.status === 'error'
    ? 'Error'
    : 'Running…';

  return (
    <div className={`bg-gray-900 border ${statusColor} rounded-lg p-6 transition-colors`}>
      <div className="flex items-center justify-between mb-3">
        <h2 className="text-lg font-semibold">{name}</h2>
        <span
          className={`text-sm px-2 py-1 rounded ${
            !result
              ? 'bg-gray-800 text-gray-400'
              : result.status === 'complete'
              ? 'bg-green-900/50 text-green-300'
              : result.status === 'error'
              ? 'bg-red-900/50 text-red-300'
              : 'bg-yellow-900/50 text-yellow-300'
          }`}
        >
          {statusLabel}
        </span>
      </div>

      {result?.error && (
        <p className="text-red-400 text-sm">{result.error}</p>
      )}

      {result?.result && (
        <pre className="text-gray-300 text-sm overflow-auto max-h-64 bg-gray-800 rounded p-3 mt-2">
          {typeof result.result === 'string'
            ? result.result
            : JSON.stringify(result.result, null, 2)}
        </pre>
      )}
    </div>
  );
}
