import { useEffect, useRef, useState } from "react";
import { useLocation, useNavigate } from "react-router-dom";

interface AgentEvent {
  agent: string;
  content: string;
}

export default function AnalysisPage() {
  const location = useLocation();
  const navigate = useNavigate();
  const repoUrl: string = (location.state as { repoUrl?: string })?.repoUrl ?? "";

  const [events, setEvents] = useState<AgentEvent[]>([]);
  const [streaming, setStreaming] = useState(false);
  const [done, setDone] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const startedRef = useRef(false);

  useEffect(() => {
    if (!repoUrl) {
      navigate("/");
      return;
    }

    if (startedRef.current) return;
    startedRef.current = true;

    // Kick off streaming in a microtask so setState calls are not
    // synchronous within the effect body (fixes react-hooks/set-state-in-effect).
    const controller = new AbortController();

    Promise.resolve().then(() => {
      setStreaming(true);
      setError(null);
      setDone(false);
      setEvents([]);

      const url = `/api/analyze/stream?repo_url=${encodeURIComponent(repoUrl)}`;
      const eventSource = new EventSource(url);

      eventSource.onmessage = (e) => {
        try {
          const data: AgentEvent = JSON.parse(e.data);
          setEvents((prev) => [...prev, data]);
        } catch {
          // ignore malformed frames
        }
      };

      eventSource.addEventListener("done", () => {
        setDone(true);
        setStreaming(false);
        eventSource.close();
      });

      eventSource.onerror = () => {
        setError("Stream connection lost. Please try again.");
        setStreaming(false);
        eventSource.close();
      };

      controller.signal.addEventListener("abort", () => {
        eventSource.close();
      });
    });

    return () => {
      controller.abort();
    };
  }, [repoUrl, navigate]);

  return (
    <div className="analysis-page">
      <h1>Analysing repository…</h1>
      {streaming && <p className="status">Streaming results…</p>}
      {error && <p className="error">{error}</p>}
      <ul className="event-list">
        {events.map((ev, i) => (
          <li key={i}>
            <strong>{ev.agent}:</strong> {ev.content}
          </li>
        ))}
      </ul>
      {done && <p className="status done">Analysis complete.</p>}
    </div>
  );
}
