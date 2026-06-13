import { useState } from 'react';
import { motion } from 'framer-motion';
import { Download, Share2, RotateCcw, AlertTriangle, Check, X, Clock, Compass, ShieldCheck, FlaskConical, Radar, LayoutGrid, ChevronRight, TrendingUp, Sparkles } from 'lucide-react';
import { ScoreRing } from '../components/ui/ScoreRing';
import { VerdictPill, toVerdict } from '../components/ui/VerdictPill';
import { RadarBg } from '../components/ui/RadarBg';
import { ActuateButton } from '../components/ui/ActuateButton';
import { AGENTS } from '../lib/agents';
import type { ShipMateReport, SecurityFinding, RepoLensSummary } from '../types';

/* ---------- Actuate context helpers ---------- */
function makeContext(report: ShipMateReport): RepoLensSummary {
  const rl = report.agents.repo_lens;
  return {
    primary_language: rl.primary_language,
    tech_stack: rl.tech_stack,
    entry_points: rl.entry_points,
    has_ci_cd: rl.has_ci_cd,
    has_tests: rl.has_tests,
  };
}

interface ActuateCtx {
  owner: string;
  repo: string;
  branch: string;
  accessToken: string | null;
  context: RepoLensSummary;
}

/* ---------- Helpers ---------- */
function pTone(p: string) { return p === 'high' || p === 'critical' ? 'red' : p === 'medium' ? 'amber' : 'blue'; }
const SEV_TONE: Record<string, string> = { critical: 'red', high: 'orange', medium: 'amber', low: 'blue', info: 'slate' };
const SEV_HEX: Record<string, string>  = { critical: '#ef4444', high: '#f97316', medium: '#f59e0b', low: '#3b82f6', info: '#64748b' };

/* ---------- AI-discovered badge + rationale tooltip ---------- */
function AIDiscoveredBadge({ rationale }: { rationale?: string | null }) {
  return (
    <span
      title={rationale || 'Discovered by AI from real repo code'}
      style={{
        display: 'inline-flex', alignItems: 'center', gap: 4,
        padding: '3px 8px', borderRadius: 999, fontSize: 10.5, fontWeight: 700,
        color: '#c4b5fd',
        background: 'linear-gradient(135deg, rgba(139,92,246,0.16), rgba(59,130,246,0.16))',
        border: '1px solid rgba(139,92,246,0.4)',
        textTransform: 'uppercase', letterSpacing: '0.04em',
        cursor: rationale ? 'help' : 'default',
      }}
    >
      <Sparkles size={10} /> AI
    </span>
  );
}

function RationaleNote({ rationale }: { rationale?: string | null }) {
  if (!rationale) return null;
  return (
    <div style={{
      marginTop: 8, padding: '8px 10px', borderRadius: 8,
      fontSize: 12, lineHeight: 1.5,
      color: '#c4b5fd',
      background: 'linear-gradient(135deg, rgba(139,92,246,0.06), rgba(59,130,246,0.06))',
      border: '1px solid rgba(139,92,246,0.22)',
      // Break long backticked file paths inside narrow card columns instead
      // of running off the right edge.
      overflowWrap: 'anywhere',
      wordBreak: 'break-word',
      minWidth: 0,
    }}>
      <span style={{ display: 'inline-flex', alignItems: 'center', gap: 4, fontWeight: 700, marginRight: 6, color: '#a78bfa' }}>
        <Sparkles size={11} /> Why:
      </span>
      <span style={{ color: 'var(--ink-2)' }}>{rationale}</span>
    </div>
  );
}

/* ---------- Executive header ---------- */
function ExecutiveReportHeader({ report, onReRun }: { report: ShipMateReport; onReRun: () => void }) {
  const verdict = toVerdict(report.ship_recommendation);
  const topBlocker = report.key_blockers[0] ?? 'No critical blockers detected.';
  return (
    <div className="card glow-border" style={{ position: 'relative', overflow: 'hidden' }}>
      <RadarBg sweep rings blobs={false} style={{ opacity: 0.3 }} />
      <div style={{ position: 'relative', display: 'grid', gridTemplateColumns: 'auto 1fr auto', gap: 28, padding: 26, alignItems: 'center' }}>
        <ScoreRing value={report.readiness_score} size={130} stroke={10} label={verdict.toUpperCase()} />

        <div>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap', marginBottom: 8 }}>
            <span className="eyebrow">Ship Report</span>
            <span className="mono muted" style={{ fontSize: 11 }}>· {new Date(report.generated_at).toLocaleString('en-US', { month: 'short', day: 'numeric', hour: '2-digit', minute: '2-digit' })}</span>
          </div>
          <h1 style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', fontSize: 26, fontWeight: 840, margin: '0 0 10px', letterSpacing: '-0.025em', color: '#fff' }}>
            <span className="mono">{report.repo.owner}/{report.repo.name}</span>
            <VerdictPill verdict={verdict} />
          </h1>
          <div style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: '11px 14px', borderRadius: 11, background: 'rgba(239,68,68,0.08)', border: '1px solid rgba(239,68,68,0.22)', maxWidth: 620 }}>
            <AlertTriangle size={16} style={{ color: '#f87171', flexShrink: 0 }} />
            <div>
              <span style={{ fontSize: 11, fontWeight: 700, color: '#fca5a5', marginRight: 8 }}>TOP BLOCKER</span>
              <span style={{ fontSize: 13, color: 'var(--ink-2)' }}>{topBlocker}</span>
            </div>
          </div>
        </div>

        <div style={{ display: 'flex', flexDirection: 'column', gap: 8, alignSelf: 'flex-start' }}>
          <button className="btn btn-secondary btn-sm"><Download size={14} /> Download PDF</button>
          <button className="btn btn-secondary btn-sm"><Share2 size={14} /> Share</button>
          <button className="btn btn-primary btn-sm" onClick={onReRun}><RotateCcw size={14} /> Re-run</button>
        </div>
      </div>
    </div>
  );
}

/* ---------- Overview tab ---------- */
function OverviewTab({ report }: { report: ShipMateReport }) {
  const verdict = toVerdict(report.ship_recommendation);
  const counts: Record<string, number> = { critical: 0, high: 0, medium: 0, low: 0 };
  report.agents.guardrail.findings.forEach((f: SecurityFinding) => { if (f.severity in counts) counts[f.severity]++; });
  const breakdown = [
    { key: 'repolens',  label: 'Repo Health', score: report.score_breakdown.repo_score },
    { key: 'planforge', label: 'Delivery',    score: report.score_breakdown.delivery_score },
    { key: 'guardrail', label: 'Security',    score: report.score_breakdown.security_score },
    { key: 'testpilot', label: 'Testing',     score: report.score_breakdown.test_score },
  ];
  const agentChip = (text: string) => {
    if (text.toLowerCase().includes('security') || text.toLowerCase().includes('secret') || text.toLowerCase().includes('cors')) return 'GuardRail';
    if (text.toLowerCase().includes('test') || text.toLowerCase().includes('coverage')) return 'TestPilot';
    if (text.toLowerCase().includes('delivery') || text.toLowerCase().includes('deploy')) return 'PlanForge';
    return 'RepoLens';
  };
  const agentTone = (name: string) => ({ GuardRail: 'amber', TestPilot: 'cyan', PlanForge: 'purple', RepoLens: 'emerald' }[name] ?? 'blue');

  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Top 3 cards */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 16 }}>
        <div className="card" style={{ padding: 20, display: 'flex', alignItems: 'center', gap: 16 }}>
          <ScoreRing value={report.readiness_score} size={92} stroke={8} />
          <div>
            <div className="eyebrow">Readiness</div>
            <div style={{ fontSize: 20, fontWeight: 800, color: '#fff', margin: '4px 0' }}>{verdict}</div>
            <div className="muted" style={{ fontSize: 12 }}>Synthesized by Ship Report</div>
          </div>
        </div>
        <div className="card" style={{ padding: 20 }}>
          <div className="eyebrow" style={{ marginBottom: 12 }}>Verdict</div>
          <VerdictPill verdict={verdict} />
          <p className="muted" style={{ fontSize: 12.5, lineHeight: 1.5, margin: '12px 0 0' }}>
            {report.key_blockers.length > 0 ? `Address ${report.key_blockers.length} blocker${report.key_blockers.length > 1 ? 's' : ''} before deploying.` : 'All critical checks passed. Ready to deploy.'}
          </p>
        </div>
        <div className="card" style={{ padding: 20 }}>
          <div className="eyebrow" style={{ marginBottom: 14 }}>Issues by severity</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {Object.entries(counts).map(([k, n]) => (
              <div key={k} style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12.5, color: 'var(--ink-2)', textTransform: 'capitalize' }}>
                  <span className="dot" style={{ background: SEV_HEX[k] ?? '#64748b' }} /> {k}
                </span>
                <span className="mono" style={{ fontSize: 13, fontWeight: 700, color: '#fff' }}>{n}</span>
              </div>
            ))}
          </div>
        </div>
      </div>

      {/* Score breakdown */}
      <div className="card" style={{ padding: 20 }}>
        <div className="eyebrow" style={{ marginBottom: 18 }}>Score breakdown</div>
        <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 20 }}>
          {breakdown.map(b => {
            const agent = AGENTS.find(a => a.key === b.key);
            if (!agent) return null;
            return (
              <div key={b.key} style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 10 }}>
                <ScoreRing value={b.score} size={84} stroke={7} />
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <agent.Icon size={14} style={{ color: agent.hex }} />
                  <span style={{ fontSize: 12.5, fontWeight: 650, color: 'var(--ink-2)' }}>{b.label}</span>
                </div>
              </div>
            );
          })}
        </div>
      </div>

      {/* Recommended actions */}
      {report.next_actions.length > 0 && (
        <div className="card" style={{ padding: 20 }}>
          <div className="eyebrow" style={{ marginBottom: 16 }}>Recommended actions</div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
            {report.next_actions.slice(0, 5).map((text, i) => {
              const ag = agentChip(text);
              const pri = i === 0 ? 'high' : i < 3 ? 'medium' : 'low';
              return (
                <div key={i} style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 14px', borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid var(--line)' }}>
                  <div style={{ width: 26, height: 26, borderRadius: 8, background: 'rgba(255,255,255,0.05)', display: 'grid', placeItems: 'center', fontWeight: 800, fontSize: 12.5, color: 'var(--ink-2)', flexShrink: 0 }}>{i + 1}</div>
                  <div style={{ flex: 1, fontSize: 13.5, color: 'var(--ink-2)', lineHeight: 1.45 }}>{text}</div>
                  <span className={`chip tone-${agentTone(ag)}`}>{ag}</span>
                  <span className={`chip tone-${pTone(pri)}`} style={{ textTransform: 'capitalize' }}>{pri}</span>
                </div>
              );
            })}
          </div>
        </div>
      )}
    </div>
  );
}

/* ---------- RepoLens tab ---------- */
function RepoLensTab({ report }: { report: ShipMateReport }) {
  const rl = report.agents.repo_lens;
  const depCount = Object.values(rl.dependency_summary).reduce((s, arr) => s + (arr as string[]).length, 0);
  const archChecks = [
    { label: 'CI/CD',       on: rl.has_ci_cd      },
    { label: 'Docker',      on: rl.has_dockerfile  },
    { label: 'Tests',       on: rl.has_tests       },
  ];
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
        <div className="card" style={{ padding: 20 }}>
          <div className="eyebrow" style={{ marginBottom: 14 }}>Tech stack</div>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {rl.tech_stack.map(s => <span key={s} className="chip tone-emerald mono">{s}</span>)}
          </div>
          <div className="eyebrow" style={{ margin: '20px 0 12px' }}>Architecture</div>
          <p style={{ fontSize: 13.5, color: 'var(--ink-2)', margin: '0 0 12px' }}>{rl.architecture_pattern}</p>
          <div style={{ display: 'flex', flexWrap: 'wrap', gap: 8 }}>
            {archChecks.map(x => (
              <span key={x.label} className={`chip ${x.on ? 'tone-emerald' : 'tone-slate'}`}>
                {x.on ? <Check size={12} /> : <X size={12} />} {x.label}
              </span>
            ))}
          </div>
        </div>
        <div className="card" style={{ padding: 20 }}>
          <div className="eyebrow" style={{ marginBottom: 14 }}>Dependencies</div>
          <div style={{ display: 'flex', gap: 12 }}>
            {[['Total', depCount, 'blue'], ['Outdated', 0, 'amber'], ['Vulnerable', 0, 'red']].map(([k, v, t]) => (
              <div key={k as string} style={{ flex: 1, padding: 14, borderRadius: 12, background: 'rgba(255,255,255,0.02)', border: '1px solid var(--line)' }}>
                <div style={{ fontSize: 22, fontWeight: 820, color: `var(--${t})` }}>{v}</div>
                <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>{k}</div>
              </div>
            ))}
          </div>
          {rl.architecture_risks.length > 0 && (
            <>
              <div className="eyebrow" style={{ margin: '20px 0 12px' }}>Architecture risks</div>
              <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {rl.architecture_risks.map((r, i) => (
                  <div key={i} style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: 12.5, color: 'var(--ink-2)', lineHeight: 1.5 }}>
                    <AlertTriangle size={14} style={{ color: 'var(--amber)', flexShrink: 0, marginTop: 1 }} /> {r.risk}
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      </div>
    </div>
  );
}

/* ---------- PlanForge tab ---------- */
function PlanForgeTab({ report, ctx }: { report: ShipMateReport; ctx: ActuateCtx }) {
  const pf = report.agents.plan_forge;
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div className="card" style={{ display: 'flex', alignItems: 'flex-start', gap: 12, padding: 18, background: 'rgba(139,92,246,0.07)', borderColor: 'rgba(139,92,246,0.25)' }}>
        <Compass size={20} style={{ color: 'var(--purple)', flexShrink: 0 }} />
        <div style={{ flex: 1 }}>
          <div style={{ fontSize: 11, fontWeight: 700, color: '#c4b5fd', textTransform: 'uppercase', letterSpacing: '0.05em', marginBottom: 3 }}>Next best action</div>
          <div style={{ fontSize: 14, color: 'var(--ink-2)', lineHeight: 1.5 }}>{pf.next_best_action}</div>
        </div>
        <ActuateButton
          owner={ctx.owner} repo={ctx.repo} branch={ctx.branch}
          accessToken={ctx.accessToken} context={ctx.context}
          finding={{
            kind: 'next_action',
            id: 'next-best-action',
            title: pf.next_best_action.slice(0, 80),
            description: pf.next_best_action,
            recommendation: pf.next_best_action,
            category: 'feature',
          }}
          label="Run It"
        />
      </div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit,minmax(240px,1fr))', gap: 14 }}>
        {pf.milestones.map((m, i) => {
          const isAI = m.source === 'discovery';
          return (
            <div key={i} className="card card-hover" style={{
              padding: 18, display: 'flex', flexDirection: 'column', gap: 10,
              borderColor: isAI ? 'rgba(139,92,246,0.32)' : undefined,
              background: isAI ? 'linear-gradient(180deg, rgba(139,92,246,0.04), var(--panel))' : undefined,
            }}>
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 6 }}>
                <span className="chip tone-purple">{m.category}</span>
                <div style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  {isAI && <AIDiscoveredBadge rationale={m.rationale} />}
                  <span className={`chip tone-${pTone(m.priority)}`} style={{ textTransform: 'capitalize' }}>{m.priority}</span>
                </div>
              </div>
              <div style={{ fontSize: 15, fontWeight: 700, color: '#fff', lineHeight: 1.35 }}>{m.title}</div>
              {m.description && (
                <div className="muted" style={{ fontSize: 12.5, lineHeight: 1.5 }}>{m.description}</div>
              )}
              <RationaleNote rationale={m.rationale} />
              <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 4, fontSize: 12, color: 'var(--ink-3)' }}>
                <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}>
                  <Clock size={13} /> ~{m.estimated_days} days · est.
                </span>
                <ActuateButton
                  owner={ctx.owner} repo={ctx.repo} branch={ctx.branch}
                  accessToken={ctx.accessToken} context={ctx.context}
                  finding={{
                    kind: 'milestone',
                    id: m.title.toLowerCase().replace(/[^a-z0-9]+/g, '-').slice(0, 40),
                    title: m.title,
                    description: m.description || m.title,
                    recommendation: m.rationale || m.description || '',
                    category: m.category,
                    severity: m.priority,
                  }}
                  label="Build It"
                />
              </div>
            </div>
          );
        })}
      </div>
    </div>
  );
}

/* ---------- GuardRail tab ---------- */
function GuardRailTab({ report, ctx }: { report: ShipMateReport; ctx: ActuateCtx }) {
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
      {report.agents.guardrail.findings.length === 0 ? (
        <div className="card" style={{ padding: 40, textAlign: 'center' }}>
          <p style={{ color: 'var(--emerald)', fontWeight: 600, fontSize: 14 }}>✓ No security findings detected</p>
        </div>
      ) : report.agents.guardrail.findings.map((f, i) => {
        const tone = SEV_TONE[f.severity] ?? 'slate';
        const hex  = SEV_HEX[f.severity]  ?? '#64748b';
        const isAI = f.source === 'discovery';
        return (
          <div key={i} className="card" style={{ padding: 18, borderColor: `${hex}44`, background: `linear-gradient(180deg, ${hex}0a, var(--panel))` }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 8, flexWrap: 'wrap', gap: 8 }}>
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                <span className={`chip tone-${tone}`} style={{ textTransform: 'uppercase', fontWeight: 700 }}>
                  <ShieldCheck size={12} /> {f.severity}
                </span>
                <span style={{ fontSize: 15, fontWeight: 700, color: '#fff' }}>{f.title}</span>
                {isAI && <AIDiscoveredBadge rationale={f.rationale} />}
              </div>
              {f.file && <span className="mono muted" style={{ fontSize: 11.5 }}>{f.file}</span>}
            </div>
            <p className="muted" style={{ fontSize: 13, lineHeight: 1.55, margin: '0 0 10px' }}>{f.description}</p>
            <RationaleNote rationale={f.rationale} />
            <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', gap: 12, flexWrap: 'wrap', marginTop: f.rationale ? 10 : 0 }}>
              <div style={{ display: 'flex', alignItems: 'flex-start', gap: 8, fontSize: 13, color: '#6ee7b7', flex: 1, minWidth: 240 }}>
                <ChevronRight size={14} style={{ flexShrink: 0, marginTop: 2 }} />
                <span><b style={{ color: '#a7f3d0' }}>Fix: </b>{f.recommendation}</span>
              </div>
              <ActuateButton
                owner={ctx.owner} repo={ctx.repo} branch={ctx.branch}
                accessToken={ctx.accessToken} context={ctx.context}
                finding={{
                  kind: 'guardrail',
                  id: f.id,
                  title: f.title,
                  description: f.description,
                  recommendation: f.recommendation || f.rationale || '',
                  file: f.file,
                  severity: f.severity,
                  category: f.category,
                }}
                label="Apply Fix"
              />
            </div>
          </div>
        );
      })}
    </div>
  );
}

/* ---------- TestPilot tab ---------- */
function TestPilotTab({ report, ctx }: { report: ShipMateReport; ctx: ActuateCtx }) {
  const tp = report.agents.testpilot;
  const qaColor = tp.qa_readiness === 'ready' ? '#10b981' : tp.qa_readiness === 'partial' ? '#f59e0b' : '#ef4444';
  return (
    <div style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(3,1fr)', gap: 16 }}>
        {[
          { k: 'Tests',      v: tp.existing_tests.count,                                      tone: 'cyan',    Icon: FlaskConical },
          { k: 'Coverage',   v: `${tp.existing_tests.coverage_estimate}%`,                    tone: 'blue',    Icon: TrendingUp   },
          { k: 'QA Status',  v: tp.qa_readiness === 'ready' ? 'QA Ready' : tp.qa_readiness === 'partial' ? 'Partial' : 'Not Ready', tone: 'amber', Icon: ShieldCheck },
        ].map(item => (
          <div key={item.k} className="card" style={{ padding: 20 }}>
            <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
              <div style={{ width: 36, height: 36, borderRadius: 10, background: `var(--${item.tone})18`, border: '1px solid var(--line-2)', color: `var(--${item.tone})`, display: 'grid', placeItems: 'center' }}>
                <item.Icon size={18} />
              </div>
              {item.k === 'Coverage' && <span className="chip tone-emerald"><TrendingUp size={11} /> +{tp.suggested_tests.length * 3}%</span>}
            </div>
            <div style={{ fontSize: 24, fontWeight: 820, color: item.k === 'QA Status' ? qaColor : '#fff', marginTop: 14 }}>{item.v}</div>
            <div className="muted" style={{ fontSize: 11.5, marginTop: 2 }}>{item.k}</div>
          </div>
        ))}
      </div>
      <div className="card" style={{ padding: 20 }}>
        <div className="eyebrow" style={{ marginBottom: 16 }}>Suggested tests · {tp.suggested_tests.length} suites</div>
        <div style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
          {tp.suggested_tests.map((t, i) => {
            const isAI = t.source === 'discovery';
            return (
              <div key={i} style={{
                display: 'flex', flexDirection: 'column', gap: 8,
                padding: '13px 14px', borderRadius: 12,
                background: isAI ? 'linear-gradient(180deg, rgba(139,92,246,0.05), rgba(255,255,255,0.02))' : 'rgba(255,255,255,0.02)',
                border: `1px solid ${isAI ? 'rgba(139,92,246,0.28)' : 'var(--line)'}`,
              }}>
                <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 10 }}>
                  <div style={{ minWidth: 0, flex: 1 }}>
                    <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span className="mono" style={{ fontSize: 13.5, fontWeight: 650, color: '#fff', wordBreak: 'break-all' }}>{t.name}</span>
                      {isAI && <AIDiscoveredBadge rationale={t.rationale} />}
                    </div>
                    <div className="muted" style={{ fontSize: 12, marginTop: 3 }}>{t.description}</div>
                    {t.target_file && (
                      <div className="mono muted" style={{ fontSize: 11, marginTop: 4 }}>{t.target_file}</div>
                    )}
                  </div>
                  <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexShrink: 0 }}>
                    <span className="chip tone-cyan" style={{ textTransform: 'uppercase' }}>{t.type}</span>
                    <span className={`chip tone-${pTone(t.priority)}`} style={{ textTransform: 'capitalize' }}>{t.priority}</span>
                    <ActuateButton
                      owner={ctx.owner} repo={ctx.repo} branch={ctx.branch}
                      accessToken={ctx.accessToken} context={ctx.context}
                      finding={{
                        kind: 'test',
                        id: t.name,
                        title: t.name,
                        description: t.description,
                        recommendation: t.rationale || t.description,
                        file: t.target_file,
                        severity: t.priority,
                        category: 'testing',
                      }}
                      label="Generate Test"
                    />
                  </div>
                </div>
                <RationaleNote rationale={t.rationale} />
              </div>
            );
          })}
        </div>
      </div>
    </div>
  );
}

/* ---------- Main ---------- */
const TABS = [
  { key: 'overview',  label: 'Overview',  Icon: LayoutGrid  },
  { key: 'repolens',  label: 'RepoLens',  Icon: Radar        },
  { key: 'planforge', label: 'PlanForge', Icon: Compass      },
  { key: 'guardrail', label: 'GuardRail', Icon: ShieldCheck  },
  { key: 'testpilot', label: 'TestPilot', Icon: FlaskConical },
] as const;

interface Props {
  report: ShipMateReport;
  onReRun: () => void;
  accessToken: string | null;
}

export function ReportsPage({ report, onReRun, accessToken }: Props) {
  const [tab, setTab] = useState<typeof TABS[number]['key']>('overview');

  const ctx: ActuateCtx = {
    owner: report.repo.owner,
    repo: report.repo.name,
    branch: report.repo.branch || 'main',
    accessToken,
    context: makeContext(report),
  };

  return (
    <motion.div className="page-content" initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55, ease: [0.2,0.7,0.2,1] }}
      style={{ padding: '28px 32px', maxWidth: 1180, margin: '0 auto', width: '100%' }}>
      <ExecutiveReportHeader report={report} onReRun={onReRun} />

      {/* Heuristic-only banner: when the LLM discovery path didn't run (expired
          creds / unreachable provider), findings are generic heuristics — say
          so instead of letting the user wonder why they look template-y. */}
      {report.ai_enhanced === false && (
        <div className="card" style={{
          marginTop: 16, padding: '12px 16px', display: 'flex', alignItems: 'center', gap: 10,
          background: 'rgba(245,158,11,0.08)', borderColor: 'rgba(245,158,11,0.3)',
        }}>
          <span className="dot" style={{ background: '#f59e0b' }} />
          <span style={{ fontSize: 13, color: 'var(--ink-2)' }}>
            <strong style={{ color: '#fbbf24' }}>Heuristic-only mode</strong> — AI
            discovery was unavailable for this run (LLM provider not reachable),
            so findings are deterministic heuristics rather than code-grounded AI
            analysis. Refresh credentials and re-run for the full analysis.
          </span>
        </div>
      )}

      {/* Tab bar */}
      <div style={{ display: 'flex', gap: 16, borderBottom: '1px solid var(--line)', margin: '22px 0', overflowX: 'auto' }}>
        {TABS.map(t => (
          <button key={t.key} className={`tab ${tab === t.key ? 'active' : ''}`} onClick={() => setTab(t.key)}>
            <span style={{ display: 'flex', alignItems: 'center', gap: 6 }}><t.Icon size={15} /> {t.label}</span>
          </button>
        ))}
      </div>

      {/* Tab content */}
      <motion.div key={tab} initial={{ opacity: 0, y: 8 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.2 }}>
        {tab === 'overview'  && <OverviewTab  report={report} />}
        {tab === 'repolens'  && <RepoLensTab  report={report} />}
        {tab === 'planforge' && <PlanForgeTab report={report} ctx={ctx} />}
        {tab === 'guardrail' && <GuardRailTab report={report} ctx={ctx} />}
        {tab === 'testpilot' && <TestPilotTab report={report} ctx={ctx} />}
      </motion.div>
    </motion.div>
  );
}
