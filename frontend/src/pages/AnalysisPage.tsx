import { useState, useEffect, useRef } from 'react';
import { motion } from 'framer-motion';
import { X, Rocket, Check } from 'lucide-react';
import { AGENTS } from '../lib/agents';
import { AgentCard } from '../components/ui/AgentCard';
import { LiveActivityLog } from '../components/ui/LiveActivityLog';
import { Spinner } from '../components/ui/GitHubConnectButton';
import { RadarBg } from '../components/ui/RadarBg';
import type { LogLine } from '../components/ui/LiveActivityLog';
import type { GitHubRepo, GitHubPR, AgentProgress } from '../types';

/* ---------- Pipeline Radar ---------- */
function PipelineRadar({ statuses }: { statuses: string[] }) {
  const size = 300, c = size / 2, R = 110;
  const positions = AGENTS.map((_, i) => {
    const ang = (-90 + i * 90) * Math.PI / 180;
    return { x: c + R * Math.cos(ang), y: c + R * Math.sin(ang) };
  });

  return (
    <div style={{ position: 'relative', width: size, height: size, margin: '0 auto' }}>
      {/* Compass rings */}
      {[1, 0.66, 0.33].map((f, i) => (
        <div key={i} className="compass-ring" style={{
          position: 'absolute',
          width: R * 2 * f, height: R * 2 * f,
          top: c - R * f, left: c - R * f,
          borderColor: 'rgba(96,165,250,0.14)',
        }} />
      ))}

      {/* Radar sweep */}
      <div style={{ position: 'absolute', left: c - R, top: c - R, width: R * 2, height: R * 2 }}>
        <div className="radar-sweep" style={{ opacity: 0.5 }} />
      </div>

      {/* SVG connector lines */}
      <svg width={size} height={size} style={{ position: 'absolute', inset: 0 }}>
        {positions.map((p, i) => {
          const st = statuses[i];
          return (
            <line key={i} x1={c} y1={c} x2={p.x} y2={p.y}
              stroke={st !== 'pending' ? AGENTS[i].hex : 'rgba(255,255,255,0.08)'}
              strokeWidth="1.5" strokeDasharray="3 4"
              style={{ transition: 'stroke .4s', opacity: st === 'complete' ? 0.8 : st === 'running' ? 0.6 : 0.3 }}
            />
          );
        })}
      </svg>

      {/* Center rocket */}
      <div style={{
        position: 'absolute', left: c - 30, top: c - 30, width: 60, height: 60,
        borderRadius: 16, display: 'grid', placeItems: 'center',
        background: 'linear-gradient(135deg,#2563eb,#0e7490)',
        border: '1px solid rgba(255,255,255,0.25)',
        boxShadow: '0 0 30px rgba(37,99,235,0.6)',
        animation: 'float 3s ease-in-out infinite',
      }}>
        <Rocket size={28} style={{ color: '#fff' }} />
      </div>

      {/* Agent nodes */}
      {AGENTS.map((agent, i) => {
        const p = positions[i];
        const st = statuses[i];
        return (
          <div key={agent.key} style={{
            position: 'absolute', left: p.x - 27, top: p.y - 27, width: 54, height: 54,
            borderRadius: 15, display: 'grid', placeItems: 'center',
            background: st === 'pending' ? 'rgba(255,255,255,0.03)' : `${agent.hex}1f`,
            border: `1.5px solid ${st === 'pending' ? 'var(--line-2)' : agent.hex}`,
            color: st === 'pending' ? 'var(--ink-4)' : agent.hex,
            boxShadow: st === 'running' ? `0 0 22px ${agent.hex}88` : 'none',
            transition: 'all .4s ease',
            transform: st === 'running' ? 'scale(1.12)' : 'scale(1)',
          }}>
            {st === 'running'  ? <Spinner size={20} dark={false} />
             : st === 'complete' ? <Check size={22} />
             : <agent.Icon size={20} />}
          </div>
        );
      })}
    </div>
  );
}

/* ---------- Log lines generator ---------- */
const LOG_SCRIPT: Omit<LogLine, 'time'>[] = [
  { agent: 'system',   text: 'Cloning repository @ main…',                           tone: 'slate'   },
  { agent: 'RepoLens', text: 'scanning dependency graph…',                           tone: 'emerald' },
  { agent: 'RepoLens', text: 'detected React, TypeScript, Vite configuration',       tone: 'emerald' },
  { agent: 'RepoLens', text: 'architecture pattern identified: SPA',                 tone: 'emerald' },
  { agent: 'RepoLens', text: 'dependency scan complete: 1,184 packages',             tone: 'emerald' },
  { agent: 'GuardRail', text: 'checking exposed secrets & CVEs…',                   tone: 'amber'   },
  { agent: 'GuardRail', text: '⚠ potential secret found in env.example',            tone: 'red'     },
  { agent: 'GuardRail', text: 'scanning CORS configuration…',                       tone: 'amber'   },
  { agent: 'GuardRail', text: 'dependency vulnerability scan complete',              tone: 'amber'   },
  { agent: 'PlanForge', text: 'analysing commit history & open issues…',            tone: 'purple'  },
  { agent: 'PlanForge', text: 'generating delivery milestones…',                    tone: 'purple'  },
  { agent: 'PlanForge', text: 'next best action identified',                        tone: 'purple'  },
  { agent: 'TestPilot', text: 'discovering test suites…',                           tone: 'cyan'    },
  { agent: 'TestPilot', text: 'estimating coverage gaps…',                          tone: 'cyan'    },
  { agent: 'TestPilot', text: 'generating suggested test cases',                    tone: 'cyan'    },
  { agent: 'system',   text: '✓ Readiness score computed — shipping report',        tone: 'emerald' },
];

function useActivityLog(running: boolean): LogLine[] {
  const [logs, setLogs] = useState<LogLine[]>([]);
  const idxRef = useRef(0);

  useEffect(() => {
    if (!running) {
      // Reset via a microtask to avoid synchronous setState in effect body
      const t = setTimeout(() => { setLogs([]); idxRef.current = 0; }, 0);
      return () => clearTimeout(t);
    }
    const tick = () => {
      const line = LOG_SCRIPT[idxRef.current];
      if (!line) return;
      const now = new Date();
      const hh = String(now.getHours()).padStart(2, '0');
      const mm = String(now.getMinutes()).padStart(2, '0');
      const ss = String(now.getSeconds()).padStart(2, '0');
      setLogs(prev => [...prev, { ...line, time: `${hh}:${mm}:${ss}` }]);
      idxRef.current++;
    };
    tick();
    // Spread the 16-line script across ~28s of analysis (avg ~1.75s/line) so
    // the log scroll roughly tracks the real LLM-driven pipeline.
    const id = setInterval(tick, 1750);
    return () => clearInterval(id);
  }, [running]);

  return logs;
}

/* ---------- Status mapping ---------- */
const ID_TO_INDEX: Record<string, number> = {
  repo_lens: 0, plan_forge: 1, guardrail: 2, testpilot: 3,
};
const STATUS_MAP: Record<string, string> = {
  idle: 'pending', running: 'running', complete: 'complete', error: 'error',
};

interface Props {
  selectedRepo: GitHubRepo | null;
  selectedBranch: string;
  selectedPull: GitHubPR | null;
  agents: AgentProgress[];
  /** 0..100 per agent id — matches the time-driven simulation in App.tsx. */
  progressByAgent?: Record<AgentProgress['id'], number>;
  /** 0..100 overall, capped at 95 until API resolves. */
  overallPct?: number;
  /** Real per-agent log lines from the SSE stream. When non-empty, these
   *  replace the scripted simulation so the Activity Log shows live events. */
  liveLines?: LogLine[];
  onCancel: () => void;
}

export function AnalysisPage({ selectedRepo, selectedBranch, agents, progressByAgent, overallPct, liveLines, onCancel }: Props) {
  // Use the real SSE-driven lines when present; fall back to the scripted
  // simulation only when no live lines have arrived yet (e.g. batch mode).
  const scriptedLogs = useActivityLog(!liveLines || liveLines.length === 0);
  const logs = liveLines && liveLines.length > 0 ? liveLines : scriptedLogs;

  const statuses = AGENTS.map((_, i) => {
    const match = agents.find(a => ID_TO_INDEX[a.id] === i);
    return match ? (STATUS_MAP[match.status] ?? 'pending') : 'pending';
  });

  // Use the real elapsed-time-driven overall if available; fall back to the
  // old completeCount-based calc for safety.
  const overall = overallPct ?? Math.round(
    (agents.filter(a => a.status === 'complete').length / agents.length) * 100
  );
  const activeAgent = AGENTS.find((_, i) => statuses[i] === 'running');

  return (
    <motion.div className="page-content" initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55, ease: [0.2,0.7,0.2,1] }}
      style={{ padding: '28px 32px', maxWidth: 1180, margin: '0 auto', width: '100%' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 22 }}>
        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
            <h1 style={{ fontSize: 25, fontWeight: 840, margin: 0, letterSpacing: '-0.025em', color: '#fff' }}>
              {overall >= 100 ? 'Analysis Complete' : 'Analysis in Progress'}
            </h1>
            {overall < 100 && <span className="chip tone-blue"><Spinner size={11} dark={false} /> running</span>}
          </div>
          {selectedRepo && (
            <p className="mono muted" style={{ fontSize: 13, margin: '6px 0 0' }}>
              {selectedRepo.owner.login}/{selectedRepo.name} · {selectedBranch}
            </p>
          )}
        </div>
        <button className="btn btn-danger" onClick={onCancel}>
          <X size={14} /> Cancel Analysis
        </button>
      </div>

      {/* Two-column layout */}
      <div className="analysis-grid" style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1fr) 340px', gap: 20 }}>
        {/* Left: radar + log */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div className="card glow-border radar-card" style={{ position: 'relative', overflow: 'hidden', padding: '30px 20px' }}>
            <RadarBg sweep={false} rings={false} blobs style={{ opacity: 0.5 }} />
            <div style={{ position: 'relative' }}>
              {/* Active agent pill */}
              <div style={{ display: 'flex', justifyContent: 'center', marginBottom: 8 }}>
                <span className="pill">
                  <span className="dot dot-pulse" style={{ background: activeAgent ? activeAgent.hex : '#34d399' }} />
                  {activeAgent ? `${activeAgent.name} working…` : overall >= 100 ? 'Ship Report ready' : 'Initializing crew'}
                </span>
              </div>

              <PipelineRadar statuses={statuses} />

              {/* Progress bar */}
              <div style={{ marginTop: 20 }}>
                <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
                  <span className="eyebrow">Overall progress</span>
                  <span style={{ fontWeight: 800, fontSize: 16, color: '#fff' }}>{overall}%</span>
                </div>
                <div className="track" style={{ height: 8 }}>
                  <i style={{ width: `${overall}%`, background: 'linear-gradient(90deg,#10b981,#3b82f6,#8b5cf6)', transition: 'width .5s' }} />
                </div>
              </div>
            </div>
          </div>

          <LiveActivityLog lines={logs} height={200} title="Agent Activity Log" />
        </div>

        {/* Right: per-agent cards */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {AGENTS.map((agent, i) => {
            const st = statuses[i] as 'pending' | 'running' | 'complete' | 'error';
            const agentMatch = agents.find(a => ID_TO_INDEX[a.id] === i);
            const realPct = agentMatch && progressByAgent ? progressByAgent[agentMatch.id] : undefined;
            const progress = st === 'complete' ? 100
              : st === 'error' ? 100
              : (realPct !== undefined ? realPct : (st === 'running' ? 55 : 0));
            const stats = st === 'complete' ? [
              { k: agent.key === 'repolens' ? 'deps' : agent.key === 'guardrail' ? 'findings' : agent.key === 'testpilot' ? 'tests' : 'steps', v: agent.key === 'repolens' ? '1.1k' : agent.key === 'guardrail' ? '6' : agent.key === 'testpilot' ? '4' : '4' },
              { k: 'score', v: '—', color: agent.hex },
            ] : undefined;
            return (
              <AgentCard key={agent.key} agent={agent} status={st} progress={progress} compact stats={stats} />
            );
          })}
        </div>
      </div>
    </motion.div>
  );
}
