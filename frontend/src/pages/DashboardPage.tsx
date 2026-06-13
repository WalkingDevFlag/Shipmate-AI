import { motion } from 'framer-motion';
import { AlertTriangle, FileText, RotateCcw, TrendingUp, List, ExternalLink, Lock, BookOpen } from 'lucide-react';
import { ScoreRing } from '../components/ui/ScoreRing';
import { VerdictPill, toVerdict } from '../components/ui/VerdictPill';
import { RadarBg } from '../components/ui/RadarBg';
import { Reveal } from '../components/ui/Reveal';
import { scoreColor } from '../lib/agents';
import { AGENTS } from '../lib/agents';
import type { GitHubUser, GitHubRepo, ShipMateReport } from '../types';
import type { Page } from '../components/app/AppSidebar';

interface Props {
  user: GitHubUser | null;
  repos: GitHubRepo[];
  report: ShipMateReport | null;
  onNavigate: (p: Page) => void;
  onAnalyze: (repo?: GitHubRepo) => void;
}

/* ---- Deployment Readiness hero card ---- */
function DeploymentReadinessCard({ report, onView }: { report: ShipMateReport; onView: () => void }) {
  const verdict = toVerdict(report.ship_recommendation);
  const vmap: Record<string, { hex: string; Icon: typeof AlertTriangle }> = {
    'Ready':       { hex: '#10b981', Icon: FileText },
    'Needs Fixes': { hex: '#f59e0b', Icon: AlertTriangle },
    'Blocked':     { hex: '#ef4444', Icon: AlertTriangle },
  };
  const v = vmap[verdict];
  const topBlocker = report.key_blockers[0] ?? 'No critical blockers detected.';
  const breakdown = [
    { key: 'repolens',  label: 'Repo Health', score: report.score_breakdown.repo_score },
    { key: 'planforge', label: 'Delivery',    score: report.score_breakdown.delivery_score },
    { key: 'guardrail', label: 'Security',    score: report.score_breakdown.security_score },
    { key: 'testpilot', label: 'Testing',     score: report.score_breakdown.test_score },
  ];

  return (
    <div className="card glow-border" style={{ position: 'relative', overflow: 'hidden', padding: 0 }}>
      <RadarBg sweep rings blobs={false} style={{ opacity: 0.35 }} />
      <div className="score-grid" style={{ position: 'relative', display: 'grid', gridTemplateColumns: 'auto 1fr', gap: 32, padding: 28, alignItems: 'center' }}>
        {/* Score ring */}
        <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 14, paddingRight: 28, borderRight: '1px solid var(--line)' }}>
          <div className="eyebrow">Deployment Readiness</div>
          <ScoreRing value={report.readiness_score} size={158} stroke={11} label={verdict.toUpperCase()} />
          <div className="mono" style={{ display: 'flex', alignItems: 'center', gap: 6, fontSize: 11, color: 'var(--ink-3)' }}>
            {report.repo.branch} · {report.repo.full_name.split('/')[1]}
          </div>
        </div>

        {/* Right content */}
        <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
          <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12 }}>
            <div>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
                <v.Icon size={22} style={{ color: v.hex }} />
                <h2 style={{ fontSize: 26, fontWeight: 820, margin: 0, letterSpacing: '-0.02em', color: '#fff' }}>{verdict}</h2>
              </div>
              <p className="muted" style={{ fontSize: 14, margin: 0 }}>
                {verdict === 'Ready' ? 'Cleared for deployment. ' : verdict === 'Blocked' ? 'Critical issues must be resolved. ' : 'Address blockers before you ship. '}
                <span className="mono">{report.repo.owner}/{report.repo.name}</span>
              </p>
            </div>
            <button className="btn btn-primary" onClick={onView}>
              <FileText size={15} /> Open Ship Report
            </button>
          </div>

          {/* Top blocker */}
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '12px 14px', borderRadius: 12, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.22)' }}>
            <AlertTriangle size={17} style={{ color: '#f87171', flexShrink: 0, marginTop: 1 }} />
            <div>
              <div style={{ fontSize: 11, fontWeight: 700, color: '#fca5a5', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>Top Blocker · GuardRail</div>
              <div style={{ fontSize: 13.5, color: 'var(--ink-2)', lineHeight: 1.5 }}>{topBlocker}</div>
            </div>
          </div>

          {/* Score breakdown bars */}
          <div className="stat-grid-4" style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 14 }}>
            {breakdown.map(b => {
              const agent = AGENTS.find(a => a.key === b.key);
              if (!agent) return null;
              return (
                <div key={b.key}>
                  <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 7 }}>
                    <span style={{ display: 'flex', alignItems: 'center', gap: 4, fontSize: 11.5, color: 'var(--ink-2)', fontWeight: 600 }}>
                      <agent.Icon size={13} style={{ color: agent.hex }} /> {b.label}
                    </span>
                    <span style={{ fontSize: 12.5, fontWeight: 700, color: scoreColor(b.score) }}>{b.score}</span>
                  </div>
                  <div className="track"><i style={{ width: `${b.score}%`, background: agent.hex }} /></div>
                </div>
              );
            })}
          </div>
        </div>
      </div>
    </div>
  );
}

/* ---- Agent summary row ---- */
function AgentSummaryRow({ report }: { report: ShipMateReport }) {
  const data = [
    { key: 'repolens',  stat: `${report.agents.repo_lens.file_count} files`,                   sub: report.agents.repo_lens.primary_language + ' · ' + report.agents.repo_lens.architecture_pattern.slice(0, 20), score: report.score_breakdown.repo_score },
    { key: 'planforge', stat: `${report.agents.plan_forge.milestones.length} milestones`,       sub: report.agents.plan_forge.estimated_effort,   score: report.score_breakdown.delivery_score },
    { key: 'guardrail', stat: `${report.agents.guardrail.findings.length} findings`,           sub: `score ${report.agents.guardrail.security_score}/100`, score: report.score_breakdown.security_score },
    { key: 'testpilot', stat: `${report.agents.testpilot.existing_tests.coverage_estimate}% cov`, sub: `${report.agents.testpilot.suggested_tests.length} tests suggested`, score: report.score_breakdown.test_score },
  ];

  return (
    <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(220px,1fr))', gap: 16 }}>
      {AGENTS.map((agent, i) => {
        const d = data.find(x => x.key === agent.key)!;
        return (
          <Reveal key={agent.key} delay={i * 60}>
            <div className="card card-hover" style={{ padding: 18, height: '100%' }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <div style={{ width: 38, height: 38, borderRadius: 11, display: 'grid', placeItems: 'center', background: `${agent.hex}18`, border: `1px solid ${agent.hex}44`, color: agent.hex }}>
                  <agent.Icon size={19} />
                </div>
                <span style={{ fontSize: 13, fontWeight: 700, color: scoreColor(d.score) }}>
                  {d.score}<span className="muted" style={{ fontSize: 10 }}>/100</span>
                </span>
              </div>
              <div style={{ fontSize: 14.5, fontWeight: 700, color: '#fff', marginTop: 14 }}>{agent.name}</div>
              <div style={{ fontSize: 19, fontWeight: 820, color: agent.hex, marginTop: 6 }}>{d.stat}</div>
              <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>{d.sub}</div>
            </div>
          </Reveal>
        );
      })}
    </div>
  );
}

/* ---- Recent analyses table ---- */
function RecentAnalysesTable({ repos, report, onNavigate, onAnalyze }: { repos: GitHubRepo[]; report: ShipMateReport | null; onNavigate: (p: Page) => void; onAnalyze: (r: GitHubRepo) => void }) {
  return (
    <div className="card" style={{ overflow: 'hidden' }}>
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 18px', borderBottom: '1px solid var(--line)' }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
          <List size={16} style={{ color: 'var(--ink-2)' }} />
          <span style={{ fontWeight: 700, fontSize: 14.5 }}>Recent Analyses</span>
        </div>
        <button className="btn btn-ghost btn-sm" onClick={() => onNavigate('reports')}>View all</button>
      </div>
      <div style={{ overflowX: 'auto' }}>
        <table className="tbl">
          <thead><tr>
            <th>Repository</th><th>Score</th><th>Verdict</th><th>Coverage</th><th>Last scan</th><th></th>
          </tr></thead>
          <tbody>
            {repos.slice(0, 5).map(r => {
              const isAnalyzed = report && report.repo.name === r.name;
              return (
                <tr key={r.id} style={{ cursor: 'pointer' }}
                  onClick={() => isAnalyzed && onNavigate('reports')}>
                  <td>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
                      {r.private ? <Lock size={13} style={{ color: 'var(--ink-3)' }} /> : <BookOpen size={13} style={{ color: 'var(--ink-3)' }} />}
                      <span className="mono" style={{ fontWeight: 600, color: '#fff' }}>{r.name}</span>
                    </div>
                  </td>
                  <td>{isAnalyzed ? <span style={{ fontWeight: 800, color: scoreColor(report!.readiness_score) }}>{report!.readiness_score}</span> : <span className="muted">—</span>}</td>
                  <td>{isAnalyzed ? <VerdictPill verdict={toVerdict(report!.ship_recommendation)} small /> : <span className="chip tone-slate">Not analyzed</span>}</td>
                  <td><span className="mono" style={{ color: isAnalyzed && report!.agents.testpilot.existing_tests.coverage_estimate >= 70 ? '#34d399' : 'var(--ink-2)' }}>{isAnalyzed ? `${report!.agents.testpilot.existing_tests.coverage_estimate}%` : '—'}</span></td>
                  <td><span className="muted mono" style={{ fontSize: 12 }}>{isAnalyzed ? new Date(report!.generated_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' }) : '—'}</span></td>
                  <td>
                    <button className="btn btn-ghost btn-sm" onClick={e => { e.stopPropagation(); onAnalyze(r); }}>
                      <RotateCcw size={13} />
                    </button>
                  </td>
                </tr>
              );
            })}
            {repos.length === 0 && (
              <tr><td colSpan={6} style={{ textAlign: 'center', padding: '32px 0' }}>
                <span className="muted">No repositories yet. <button onClick={() => onNavigate('repos')} style={{ color: 'var(--blue-2)', background: 'none', border: 'none', cursor: 'pointer' }}>Connect one →</button></span>
              </td></tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

/* ---- AI Insights panel ---- */
function AIInsightsPanel({ report, onView }: { report: ShipMateReport; onView: () => void }) {
  const top = report.agents.guardrail.findings[0];
  const days = [40, 62, 55, 71, 58, 66, report.readiness_score];

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card" style={{ padding: 18 }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 14 }}>
          <span style={{ color: 'var(--purple)', fontSize: 17 }}>✦</span>
          <span style={{ fontWeight: 700, fontSize: 14.5 }}>AI Insights</span>
        </div>

        {top && (
          <div style={{ padding: 14, borderRadius: 12, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.22)', marginBottom: 14 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8 }}>
              <span className="chip tone-red"><AlertTriangle size={12} /> Highest risk</span>
              {top.file && <span className="mono muted" style={{ fontSize: 11 }}>{top.file}</span>}
            </div>
            <div style={{ fontSize: 14, fontWeight: 700, color: '#fff', marginBottom: 5 }}>{top.title}</div>
            <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.5, margin: 0 }}>{top.description}</p>
          </div>
        )}

        <div style={{ padding: 14, borderRadius: 12, background: 'rgba(59,130,246,0.07)', border: '1px solid rgba(59,130,246,0.2)' }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, marginBottom: 6 }}>
            <ExternalLink size={14} style={{ color: 'var(--blue-2)' }} />
            <span style={{ fontSize: 11, fontWeight: 700, color: '#93c5fd', textTransform: 'uppercase', letterSpacing: '0.05em' }}>Next action</span>
          </div>
          <p style={{ fontSize: 13, color: 'var(--ink-2)', lineHeight: 1.5, margin: '0 0 12px' }}>{report.next_actions[0] ?? 'All guardrails passed.'}</p>
          <button className="btn btn-primary btn-sm" style={{ width: '100%' }} onClick={onView}>View full analysis →</button>
        </div>
      </div>

      {/* Trend bar chart */}
      <div className="card" style={{ padding: 18 }}>
        <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 14 }}>
          <span style={{ fontWeight: 700, fontSize: 13.5 }}>Readiness trend</span>
          <span className="chip tone-emerald"><TrendingUp size={12} /> +12 this week</span>
        </div>
        <div style={{ display: 'flex', alignItems: 'flex-end', gap: 8, height: 70 }}>
          {days.map((d, i) => (
            <div key={i} style={{
              flex: 1, height: `${d}%`, borderRadius: '5px 5px 2px 2px',
              background: i === days.length - 1 ? 'linear-gradient(180deg,#60a5fa,#2563eb)' : 'rgba(59,130,246,0.22)',
              boxShadow: i === days.length - 1 ? '0 0 14px rgba(59,130,246,0.5)' : 'none',
              transition: 'height .6s ease',
            }} />
          ))}
        </div>
        <div className="mono" style={{ display: 'flex', justifyContent: 'space-between', marginTop: 8, fontSize: 10, color: 'var(--ink-4)' }}>
          {['M','T','W','T','F','S','S'].map((d, i) => <span key={i}>{d}</span>)}
        </div>
      </div>
    </div>
  );
}

/* ---- Main ---- */
export function DashboardPage({ user, repos, report, onNavigate, onAnalyze }: Props) {
  const greeting = new Date().getHours() < 12 ? 'Good morning' : new Date().getHours() < 17 ? 'Good afternoon' : 'Good evening';
  const name = user?.name?.split(' ')[0] || user?.login || 'Developer';

  return (
    <motion.div className="page-content" initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55, ease: [0.2,0.7,0.2,1] }}
      style={{ padding: '28px 32px', maxWidth: 1180, margin: '0 auto', width: '100%' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 22 }}>
        <div>
          <h1 style={{ fontSize: 25, fontWeight: 840, margin: 0, letterSpacing: '-0.025em', color: '#fff' }}>
            {greeting}, {name}! 👋
          </h1>
          <p className="muted" style={{ fontSize: 14, margin: '5px 0 0' }}>Here's the readiness picture across your connected repositories.</p>
        </div>
        <button className="btn btn-secondary" onClick={() => onNavigate('reports')}>
          <FileText size={15} /> View Reports
        </button>
      </div>

      <div style={{ display: 'flex', flexDirection: 'column', gap: 20 }}>
        {report && <DeploymentReadinessCard report={report} onView={() => onNavigate('reports')} />}
        {report && <AgentSummaryRow report={report} />}
        <div style={{ display: 'grid', gridTemplateColumns: 'minmax(0,1.7fr) minmax(0,1fr)', gap: 20 }}>
          <RecentAnalysesTable repos={repos} report={report} onNavigate={onNavigate} onAnalyze={onAnalyze} />
          {report
            ? <AIInsightsPanel report={report} onView={() => onNavigate('reports')} />
            : <div className="card" style={{ padding: 32, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'center', textAlign: 'center', gap: 12 }}>
                <div style={{ fontSize: 40 }}>✦</div>
                <p className="muted" style={{ fontSize: 13 }}>Run your first analysis to see AI insights.</p>
                <button className="btn btn-primary btn-sm" onClick={() => onNavigate('repos')}>Go to Repositories →</button>
              </div>
          }
        </div>
      </div>
    </motion.div>
  );
}
