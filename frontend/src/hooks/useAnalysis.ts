import { useEffect, useState } from 'react';

interface AnalysisReport {
  id: number;
  repo_id: string;
  created_at: string;
  summary: string;
  details: string;
}

export function useAnalysis(repoId: string | null) {
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [history, setHistory] = useState<AnalysisReport[]>([]);

  useEffect(() => {
    if (!repoId) return;
    // Avoid calling setState synchronously in effect body
    setLoading(true);
    setError(null);
    fetch(`/api/analysis/${repoId}`)
      .then(async (res) => {
        if (!res.ok) {
          throw new Error('Failed to fetch analysis report');
        }
        const data = await res.json();
        setReport(data);
      })
      .catch((err) => {
        setError(err.message);
        setReport(null);
      })
      .finally(() => {
        setLoading(false);
      });
    fetch(`/api/analysis/${repoId}/history`)
      .then(async (res) => {
        if (!res.ok) {
          throw new Error('Failed to fetch analysis history');
        }
        const data = await res.json();
        setHistory(data);
      })
      .catch((err) => {
        setError(err.message);
        setHistory([]);
      });
  }, [repoId]);

  return { loading, error, report, history };
}
