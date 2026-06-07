import { useState, useEffect } from 'react';
import { AnalysisReport } from '../types';

export function useAnalysis(repoId: string) {
  const [report, setReport] = useState<AnalysisReport | null>(null);
  const [loading, setLoading] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!repoId) return;
    setLoading(true);
    setError(null);
    fetch(`/api/analysis/${repoId}`)
      .then(async (res) => {
        if (!res.ok) {
          throw new Error(`Failed to fetch analysis: ${res.status}`);
        }
        return res.json();
      })
      .then((data: AnalysisReport) => {
        setReport(data);
      })
      .catch((err: Error) => {
        setError(err.message);
      })
      .finally(() => {
        setLoading(false);
      });
  }, [repoId]);

  return { report, loading, error };
}
