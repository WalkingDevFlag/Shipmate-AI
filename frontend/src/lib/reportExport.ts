// Report export helpers — dependency-free.
//
// downloadReportPdf: renders a standalone, on-brand (dark "Mission Control")
// document of the full Ship Report into a hidden iframe and triggers the
// browser's print dialog, where the user picks "Save as PDF". The document
// mirrors the app's visual language — score ring, verdict pill, agent colors,
// severity-toned finding cards, radar texture — so the PDF matches the site.
// It needs no third-party PDF library.
//
// shareReport: uses the Web Share API when available (mobile / supported
// desktop), otherwise copies a verdict summary + repo link to the clipboard.

import type { ShipMateReport } from '../types';
import { toVerdict, scoreColor } from './verdict';

// ── Palette (mirrors src/index.css design tokens) ───────────────────────────
const SEV_HEX: Record<string, string> = {
  critical: '#ef4444', high: '#f97316', medium: '#f59e0b', low: '#3b82f6', info: '#64748b',
};
const SEV_TONE: Record<string, string> = {
  critical: 'red', high: 'orange', medium: 'amber', low: 'blue', info: 'slate',
};
const VERDICT_TONE: Record<string, string> = {
  Ready: 'emerald', 'Needs Fixes': 'amber', Blocked: 'red',
};

// Lucide-matching icon path data (so glyphs match the app's lucide-react icons).
const ICON = {
  search: '<circle cx="11" cy="11" r="8"/><path d="m21 21-4.3-4.3"/>',
  listChecks: '<path d="m3 17 2 2 4-4"/><path d="m3 7 2 2 4-4"/><path d="M13 6h8"/><path d="M13 12h8"/><path d="M13 18h8"/>',
  shield: '<path d="M20 13c0 5-3.5 7.5-7.66 8.95a1 1 0 0 1-.67-.01C7.5 20.5 4 18 4 13V6a1 1 0 0 1 1-1c2 0 4.5-1.2 6.24-2.72a1.17 1.17 0 0 1 1.52 0C14.51 3.81 17 5 19 5a1 1 0 0 1 1 1z"/><path d="m9 12 2 2 4-4"/>',
  flask: '<path d="M10 2v7.31"/><path d="M14 9.3V1.99"/><path d="M8.5 2h7"/><path d="M14 9.3a6.5 6.5 0 1 1-4 0"/><path d="M5.52 16h12.96"/>',
  check: '<path d="M20 6 9 17l-5-5"/>',
  alert: '<path d="m21.73 18-8-14a2 2 0 0 0-3.48 0l-8 14A2 2 0 0 0 4 21h16a2 2 0 0 0 1.73-3Z"/><path d="M12 9v4"/><path d="M12 17h.01"/>',
  x: '<path d="M18 6 6 18"/><path d="M6 6l12 12"/>',
  chevron: '<path d="m9 18 6-6-6-6"/>',
  rocket: '<path d="M4.5 16.5c-1.5 1.26-2 5-2 5s3.74-.5 5-2c.71-.84.7-2.13-.09-2.91a2.18 2.18 0 0 0-2.91 0z"/><path d="m12 15-3-3a22 22 0 0 1 2-3.95A12.88 12.88 0 0 1 22 2c0 2.72-.78 7.5-6 11a22.35 22.35 0 0 1-4 2z"/>',
};

const AGENT_META = [
  { name: 'RepoLens', label: 'Repo Health', hex: '#60a5fa', key: 'repo_score', icon: ICON.search },
  { name: 'PlanForge', label: 'Delivery', hex: '#a78bfa', key: 'delivery_score', icon: ICON.listChecks },
  { name: 'GuardRail', label: 'Security', hex: '#f87171', key: 'security_score', icon: ICON.shield },
  { name: 'TestPilot', label: 'Testing', hex: '#34d399', key: 'test_score', icon: ICON.flask },
] as const;

function esc(s: unknown): string {
  return String(s ?? '')
    .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;').replace(/'/g, '&#39;');
}

function clamp(n: number): number { return Math.max(0, Math.min(100, Number(n) || 0)); }

function svg(paths: string, size: number, color: string, sw = 2): string {
  return `<svg width="${size}" height="${size}" viewBox="0 0 24 24" fill="none" stroke="${color}" stroke-width="${sw}" stroke-linecap="round" stroke-linejoin="round" style="flex-shrink:0">${paths}</svg>`;
}

/** Static replica of the app's animated ScoreRing. */
function ring(value: number, size: number, stroke: number, label?: string): string {
  const v = clamp(value);
  const r = (size - stroke) / 2;
  const c = 2 * Math.PI * r;
  const offset = c - (v / 100) * c;
  const color = scoreColor(v);
  return `<div class="ring" style="width:${size}px;height:${size}px">
    <svg width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" style="transform:rotate(-90deg);overflow:visible">
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="rgba(255,255,255,0.08)" stroke-width="${stroke}"/>
      <circle cx="${size / 2}" cy="${size / 2}" r="${r}" fill="none" stroke="${color}" stroke-width="${stroke}" stroke-linecap="round" stroke-dasharray="${c.toFixed(1)}" stroke-dashoffset="${offset.toFixed(1)}"/>
    </svg>
    <div class="ring-c">
      <span class="ring-n" style="font-size:${Math.round(size * 0.3)}px">${Math.round(v)}</span>
      ${label ? `<span class="ring-l" style="color:${color};font-size:${Math.round(size * 0.1)}px">${esc(label)}</span>` : ''}
    </div>
  </div>`;
}

function chip(text: string, tone: string, icon?: string): string {
  return `<span class="chip tone-${tone}">${icon ? svg(icon, 11, 'currentColor') : ''}${esc(text)}</span>`;
}

function verdictPill(verdict: string): string {
  const tone = VERDICT_TONE[verdict] ?? 'amber';
  const ic = verdict === 'Ready' ? ICON.check : verdict === 'Blocked' ? ICON.x : ICON.alert;
  return `<span class="chip tone-${tone}" style="font-size:12px;padding:5px 11px">${svg(ic, 13, 'currentColor')}${esc(verdict)}</span>`;
}

function buildReportHtml(report: ShipMateReport): string {
  const verdict = toVerdict(report.ship_recommendation);
  const hasBlockers = report.key_blockers.length > 0;
  const topBlocker = report.key_blockers[0] ?? 'No critical blockers — cleared for deployment.';
  const rl = report.agents.repo_lens;
  const gr = report.agents.guardrail;
  const tp = report.agents.testpilot;
  const pf = report.agents.plan_forge;

  // Severity counts for the GuardRail summary strip.
  const counts: Record<string, number> = { critical: 0, high: 0, medium: 0, low: 0 };
  gr.findings.forEach(f => { if (f.severity in counts) counts[f.severity]++; });

  const breakdownBars = AGENT_META.map(a => {
    const score = clamp((report.score_breakdown as unknown as Record<string, number>)[a.key]);
    return `<div class="bd-row">
      <div class="bd-head">
        <span class="bd-name">${svg(a.icon, 12, a.hex)} ${esc(a.label)}</span>
        <span class="bd-score" style="color:${scoreColor(score)}">${Math.round(score)}</span>
      </div>
      <div class="track"><i style="width:${score}%;background:${a.hex}"></i></div>
    </div>`;
  }).join('');

  const sevStrip = Object.entries(counts).map(([k, n]) =>
    `<span class="sev-pip"><span class="dot" style="background:${SEV_HEX[k]}"></span>${n} ${esc(k)}</span>`).join('');

  const actionsHtml = report.next_actions.length
    ? `<section class="card pad">
        <div class="eyebrow mb">Recommended actions</div>
        <div class="stack8">
          ${report.next_actions.slice(0, 6).map((t, i) => {
            const pri = i === 0 ? 'high' : i < 3 ? 'medium' : 'low';
            return `<div class="action">
              <span class="num-badge">${i + 1}</span>
              <span class="action-text">${esc(t)}</span>
              ${chip(pri, pri === 'high' ? 'red' : pri === 'medium' ? 'amber' : 'blue')}
            </div>`;
          }).join('')}
        </div>
      </section>`
    : '';

  const findingsHtml = gr.findings.length
    ? gr.findings.map(f => {
        const hex = SEV_HEX[f.severity] ?? '#64748b';
        const tone = SEV_TONE[f.severity] ?? 'slate';
        return `<div class="card pad finding" style="border-color:${hex}55;background:linear-gradient(180deg, ${hex}12, var(--panel))">
          <div class="finding-head">
            ${chip(f.severity, tone, ICON.shield)}
            <strong>${esc(f.title)}</strong>
            ${f.file ? `<span class="mono file">${esc(f.file)}</span>` : ''}
          </div>
          <p class="muted">${esc(f.description)}</p>
          ${f.recommendation ? `<p class="fix">${svg(ICON.chevron, 13, '#6ee7b7')}<span><b>Fix:</b> ${esc(f.recommendation)}</span></p>` : ''}
        </div>`;
      }).join('')
    : `<div class="card pad center emerald">${svg(ICON.check, 16, '#34d399')} No security findings detected</div>`;

  const milestonesHtml = pf.milestones.length
    ? `<div class="grid-cards">${pf.milestones.map(m => {
        const isAI = m.source === 'discovery';
        const pTone = m.priority === 'high' || m.priority === 'critical' ? 'red' : m.priority === 'medium' ? 'amber' : 'blue';
        return `<div class="card pad ms${isAI ? ' ai' : ''}">
          <div class="ms-top">
            ${chip(m.category, 'purple')}
            <div class="ms-tags">${isAI ? '<span class="chip ai-badge">AI</span>' : ''}${chip(m.priority, pTone)}</div>
          </div>
          <div class="ms-title">${esc(m.title)}</div>
          ${m.description ? `<div class="muted small">${esc(m.description)}</div>` : ''}
          <div class="ms-foot muted small">~${esc(m.estimated_days)} days · est.</div>
        </div>`;
      }).join('')}</div>`
    : '';

  const testsHtml = tp.suggested_tests.length
    ? `<div class="stack8">${tp.suggested_tests.slice(0, 10).map(t => {
        const pTone = t.priority === 'high' || t.priority === 'critical' ? 'red' : t.priority === 'medium' ? 'amber' : 'blue';
        return `<div class="test-row">
          <div class="test-main">
            <span class="mono test-name">${esc(t.name)}</span>
            <div class="muted small">${esc(t.description)}</div>
            ${t.target_file ? `<div class="mono muted small">${esc(t.target_file)}</div>` : ''}
          </div>
          <div class="test-tags">${chip(t.type, 'cyan')}${chip(t.priority, pTone)}</div>
        </div>`;
      }).join('')}</div>`
    : '<p class="muted">No suggested tests.</p>';

  const flags = [
    ['CI/CD', rl.has_ci_cd], ['Docker', rl.has_dockerfile], ['Tests', rl.has_tests],
  ].map(([k, on]) => `<span class="chip ${on ? 'tone-emerald' : 'tone-slate'}">${svg(on ? ICON.check : ICON.x, 11, 'currentColor')}${esc(k)}</span>`).join('');

  const prRiskHtml = report.pr_risk ? (() => {
    const pr = report.pr_risk!;
    const hex = SEV_HEX[pr.risk_level] ?? '#64748b';
    return `<section class="card pad" style="border-color:${hex}55;background:linear-gradient(180deg, ${hex}10, var(--panel))">
      <div class="eyebrow mb" style="color:${hex}">PR Risk</div>
      <div class="pr-head">
        <span class="mono" style="font-weight:700;color:#fff">#${esc(pr.pr_number)}</span>
        <span class="muted">${esc(pr.title)}</span>
        ${chip(`${pr.risk_level} risk · ${pr.risk_score}/100`, SEV_TONE[pr.risk_level] ?? 'slate')}
      </div>
      ${pr.summary ? `<p class="muted">${esc(pr.summary)}</p>` : ''}
      <div class="chips-row">${chip(`${pr.files_changed} files`, 'slate')}${chip(`+${pr.additions}`, 'emerald')}${chip(`-${pr.deletions}`, 'red')}</div>
    </section>`;
  })() : '';

  return `<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8" />
<title>shipmate-report-${esc(report.repo.owner)}-${esc(report.repo.name)}</title>
<link rel="preconnect" href="https://fonts.googleapis.com" />
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800;900&family=JetBrains+Mono:wght@400;500;600;700&display=swap" rel="stylesheet" />
<style>
  :root {
    --bg: #070c18; --panel: #0d1528; --line: rgba(255,255,255,0.08); --line-2: rgba(255,255,255,0.14);
    --ink: #f3f6fc; --ink-2: #aeb9cf; --ink-3: #6f7d97; --ink-4: #6079a0;
    --blue: #3b82f6; --emerald: #10b981; --amber: #f59e0b; --red: #ef4444;
  }
  * { box-sizing: border-box; -webkit-print-color-adjust: exact; print-color-adjust: exact; }
  html, body { margin: 0; padding: 0; background: var(--bg); }
  body {
    font-family: "Inter", system-ui, -apple-system, sans-serif;
    color: var(--ink); line-height: 1.55; font-size: 13px;
    padding: 34px 38px;
  }
  .mono { font-family: "JetBrains Mono", ui-monospace, monospace; }
  .muted { color: var(--ink-3); }
  .small { font-size: 11px; }
  .mb { margin-bottom: 14px; }
  .center { text-align: center; }
  .emerald { color: #34d399; font-weight: 600; display: flex; align-items: center; justify-content: center; gap: 8px; }
  .stack8 { display: flex; flex-direction: column; gap: 8px; }

  .card { position: relative; background: var(--panel); border: 1px solid var(--line); border-radius: 16px; }
  .pad { padding: 18px 20px; }
  section { break-inside: avoid; margin-top: 16px; }

  .eyebrow { font-size: 11px; font-weight: 700; letter-spacing: 0.16em; text-transform: uppercase; color: var(--ink-3); }

  /* chips / tones — replicate index.css */
  .chip { display: inline-flex; align-items: center; gap: 5px; font-size: 11px; font-weight: 600;
    padding: 3px 9px; border-radius: 8px; border: 1px solid var(--line); color: var(--ink-2);
    background: rgba(255,255,255,0.03); text-transform: capitalize; white-space: nowrap; }
  .tone-blue { color: #bfdbfe; border-color: rgba(59,130,246,0.32); background: rgba(59,130,246,0.12); }
  .tone-cyan { color: #a5f3fc; border-color: rgba(34,211,238,0.30); background: rgba(34,211,238,0.12); }
  .tone-purple { color: #ddd6fe; border-color: rgba(139,92,246,0.32); background: rgba(139,92,246,0.12); }
  .tone-emerald { color: #a7f3d0; border-color: rgba(16,185,129,0.32); background: rgba(16,185,129,0.12); }
  .tone-amber { color: #fde68a; border-color: rgba(245,158,11,0.32); background: rgba(245,158,11,0.12); }
  .tone-orange { color: #fed7aa; border-color: rgba(249,115,22,0.32); background: rgba(249,115,22,0.12); }
  .tone-red { color: #fecaca; border-color: rgba(239,68,68,0.34); background: rgba(239,68,68,0.14); }
  .tone-slate { color: var(--ink); border-color: rgba(255,255,255,0.15); background: rgba(255,255,255,0.08); }
  .ai-badge { color: #c4b5fd; border-color: rgba(139,92,246,0.4); background: linear-gradient(135deg, rgba(139,92,246,0.18), rgba(59,130,246,0.18)); text-transform: uppercase; letter-spacing: 0.04em; font-weight: 700; }

  /* score ring */
  .ring { position: relative; flex-shrink: 0; }
  .ring-c { position: absolute; inset: 0; display: flex; flex-direction: column; align-items: center; justify-content: center; }
  .ring-n { font-weight: 800; color: #fff; line-height: 1; }
  .ring-l { font-weight: 700; margin-top: 3px; }

  /* exec header */
  .exec { overflow: hidden; border-color: rgba(96,165,250,0.22); box-shadow: 0 0 0 1px rgba(59,130,246,0.10), 0 22px 60px -30px rgba(37,99,235,0.5); }
  .radar { position: absolute; inset: 0; pointer-events: none; opacity: 0.5;
    background-image:
      radial-gradient(circle at 18% 30%, rgba(59,130,246,0.16) 0%, transparent 45%),
      linear-gradient(rgba(255,255,255,0.025) 1px, transparent 1px),
      linear-gradient(90deg, rgba(255,255,255,0.025) 1px, transparent 1px);
    background-size: 100% 100%, 40px 40px, 40px 40px; }
  .ring-deco { position: absolute; border: 1px solid rgba(96,165,250,0.12); border-radius: 50%; pointer-events: none; }
  .exec-grid { position: relative; display: grid; grid-template-columns: auto 1fr auto; gap: 26px; padding: 24px; align-items: center; }
  .exec-main { min-width: 0; }
  .repo { display: flex; align-items: center; gap: 12px; flex-wrap: wrap; font-size: 23px; font-weight: 800; letter-spacing: -0.02em; margin: 8px 0 12px; color: #fff; }
  .blocker { display: flex; align-items: flex-start; gap: 11px; padding: 11px 14px; border-radius: 11px; max-width: 560px; }
  .blocker.bad { background: rgba(239,68,68,0.10); border: 1px solid rgba(239,68,68,0.24); }
  .blocker.good { background: rgba(16,185,129,0.10); border: 1px solid rgba(16,185,129,0.24); }
  .blk-label { font-size: 11px; font-weight: 700; letter-spacing: 0.04em; margin-right: 8px; }
  .blocker.bad .blk-label { color: #fca5a5; }
  .blocker.good .blk-label { color: #6ee7b7; }
  .blk-text { font-size: 13px; color: var(--ink-2); }
  .exec-side { display: flex; flex-direction: column; gap: 10px; width: 188px; }

  /* breakdown bars */
  .bd-head { display: flex; align-items: center; justify-content: space-between; margin-bottom: 6px; }
  .bd-name { display: flex; align-items: center; gap: 5px; font-size: 11.5px; color: var(--ink-2); font-weight: 600; }
  .bd-score { font-size: 12.5px; font-weight: 700; }
  .track { height: 6px; border-radius: 999px; background: rgba(255,255,255,0.07); overflow: hidden; }
  .track > i { display: block; height: 100%; border-radius: 999px; }

  /* severity strip */
  .sev-strip { display: flex; gap: 16px; flex-wrap: wrap; }
  .sev-pip { display: flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-2); text-transform: capitalize; }
  .dot { width: 7px; height: 7px; border-radius: 50%; display: inline-block; }

  /* recommended actions */
  .action { display: flex; align-items: center; gap: 11px; padding: 11px 13px; border-radius: 11px; background: rgba(255,255,255,0.02); border: 1px solid var(--line); }
  .num-badge { width: 24px; height: 24px; flex-shrink: 0; border-radius: 7px; background: rgba(255,255,255,0.06); display: flex; align-items: center; justify-content: center; font-weight: 800; font-size: 12px; color: var(--ink-2); }
  .action-text { flex: 1; font-size: 13px; color: var(--ink-2); line-height: 1.45; }

  /* findings */
  .finding { margin-bottom: 10px; }
  .finding-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; margin-bottom: 7px; }
  .finding-head strong { font-size: 14px; color: #fff; }
  .file { font-size: 11px; color: var(--ink-3); }
  .finding p { margin: 0; font-size: 12.5px; line-height: 1.5; }
  .fix { display: flex; align-items: flex-start; gap: 6px; margin-top: 8px !important; color: #6ee7b7; font-size: 12.5px; }
  .fix b { color: #a7f3d0; }

  /* milestones */
  .grid-cards { display: grid; grid-template-columns: repeat(auto-fill, minmax(230px, 1fr)); gap: 12px; }
  .ms { display: flex; flex-direction: column; gap: 8px; }
  .ms.ai { border-color: rgba(139,92,246,0.32); background: linear-gradient(180deg, rgba(139,92,246,0.06), var(--panel)); }
  .ms-top { display: flex; align-items: center; justify-content: space-between; gap: 6px; }
  .ms-tags { display: flex; gap: 6px; }
  .ms-title { font-size: 14px; font-weight: 700; color: #fff; line-height: 1.3; }
  .ms-foot { margin-top: 2px; }

  /* tests */
  .test-row { display: flex; align-items: flex-start; justify-content: space-between; gap: 12px; padding: 11px 13px; border-radius: 11px; background: rgba(255,255,255,0.02); border: 1px solid var(--line); }
  .test-name { font-size: 13px; font-weight: 600; color: #fff; word-break: break-all; }
  .test-tags { display: flex; gap: 6px; flex-shrink: 0; }

  /* stat tiles */
  .tiles { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin-bottom: 14px; }
  .tile-v { font-size: 24px; font-weight: 800; color: #fff; margin-top: 4px; }

  .chips-row { display: flex; gap: 8px; flex-wrap: wrap; margin-top: 6px; }
  .pr-head { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; margin-bottom: 8px; }

  h2.sec { font-size: 12px; text-transform: uppercase; letter-spacing: 0.14em; color: var(--ink-3); margin: 22px 0 10px; display: flex; align-items: center; gap: 8px; }

  footer { margin-top: 26px; padding-top: 12px; border-top: 1px solid var(--line); color: var(--ink-4); font-size: 10.5px; display: flex; justify-content: space-between; }

  @page { margin: 0; size: A4; }
</style>
</head>
<body>
  <!-- Executive header -->
  <div class="exec card">
    <div class="radar"></div>
    <div class="ring-deco" style="width:300px;height:300px;left:-90px;top:-120px"></div>
    <div class="ring-deco" style="width:200px;height:200px;left:-40px;top:-70px"></div>
    <div class="exec-grid">
      <div>${ring(report.readiness_score, 132, 11, verdict.toUpperCase())}</div>
      <div class="exec-main">
        <div class="eyebrow">Ship Report <span class="mono muted" style="letter-spacing:0">· ${esc(new Date(report.generated_at).toLocaleString())}</span></div>
        <h1 class="repo"><span class="mono">${esc(report.repo.full_name)}</span> ${verdictPill(verdict)}</h1>
        <div class="blocker ${hasBlockers ? 'bad' : 'good'}">
          ${svg(hasBlockers ? ICON.alert : ICON.shield, 16, hasBlockers ? '#f87171' : '#34d399')}
          <div><span class="blk-label">${hasBlockers ? 'TOP BLOCKER' : 'ALL CLEAR'}</span><span class="blk-text">${esc(topBlocker)}</span></div>
        </div>
      </div>
      <div class="exec-side">${breakdownBars}</div>
    </div>
  </div>

  ${actionsHtml}

  <!-- RepoLens -->
  <h2 class="sec">${svg(ICON.search, 14, '#60a5fa')} RepoLens · Code Health</h2>
  <section class="card pad">
    <div style="display:flex;flex-wrap:wrap;gap:24px;margin-bottom:14px">
      <div><span class="muted small">Primary language</span><div style="font-weight:700;color:#fff">${esc(rl.primary_language)}</div></div>
      <div><span class="muted small">Files</span><div class="mono" style="font-weight:700;color:#fff">${esc(rl.file_count)}</div></div>
      <div style="flex:1;min-width:200px"><span class="muted small">Architecture</span><div style="color:var(--ink-2)">${esc(rl.architecture_pattern)}</div></div>
    </div>
    <div class="chips-row" style="margin-bottom:12px">${flags}</div>
    <div class="chips-row">${rl.tech_stack.map(s => `<span class="chip tone-emerald mono">${esc(s)}</span>`).join('')}</div>
  </section>

  <!-- GuardRail -->
  <h2 class="sec">${svg(ICON.shield, 14, '#f87171')} GuardRail · Security
    <span style="flex:1"></span>
    <span class="muted small mono" style="text-transform:none;letter-spacing:0">${esc(gr.security_score)}/100 · ${gr.findings.length} finding(s)</span>
  </h2>
  ${gr.findings.length ? `<div class="sev-strip mb">${sevStrip}</div>` : ''}
  ${findingsHtml}

  <!-- TestPilot -->
  <h2 class="sec">${svg(ICON.flask, 14, '#34d399')} TestPilot · Testing</h2>
  <section>
    <div class="tiles">
      <div class="card pad"><span class="muted small">Tests</span><div class="tile-v">${esc(tp.existing_tests.count)}</div></div>
      <div class="card pad"><span class="muted small">Est. coverage</span><div class="tile-v">${esc(tp.existing_tests.coverage_estimate)}%</div></div>
      <div class="card pad"><span class="muted small">QA readiness</span><div class="tile-v" style="text-transform:capitalize">${esc(tp.qa_readiness)}</div></div>
    </div>
    <div class="card pad">
      <div class="eyebrow mb">Suggested tests · ${tp.suggested_tests.length} total</div>
      ${testsHtml}
    </div>
  </section>

  <!-- PlanForge -->
  <h2 class="sec">${svg(ICON.listChecks, 14, '#a78bfa')} PlanForge · Delivery</h2>
  <section>
    ${pf.next_best_action ? `<div class="card pad mb" style="background:rgba(139,92,246,0.07);border-color:rgba(139,92,246,0.25)">
      <div class="eyebrow" style="color:#c4b5fd;margin-bottom:4px">Next best action</div>
      <div style="color:var(--ink-2)">${esc(pf.next_best_action)}</div>
    </div>` : ''}
    ${milestonesHtml}
  </section>

  ${prRiskHtml}

  <footer>
    <span>${svg(ICON.rocket, 11, '#60a5fa')} ShipMate AI — Deployment Readiness Report</span>
    <span>${esc(report.repo.full_name)} · ${esc(verdict)} · ${esc(report.readiness_score)}/100</span>
  </footer>
</body>
</html>`;
}

// Exported for verification/snapshotting; the app calls downloadReportPdf.
export { buildReportHtml };

/**
 * Render the full report into a hidden iframe and open the print dialog
 * (Save as PDF). Resolves once printing has been triggered.
 */
export function downloadReportPdf(report: ShipMateReport): Promise<void> {
  return new Promise((resolve, reject) => {
    try {
      const iframe = document.createElement('iframe');
      iframe.setAttribute('aria-hidden', 'true');
      Object.assign(iframe.style, {
        position: 'fixed', right: '0', bottom: '0', width: '0', height: '0', border: '0',
      } as CSSStyleDeclaration);
      document.body.appendChild(iframe);

      const doc = iframe.contentWindow?.document;
      if (!doc) {
        iframe.remove();
        reject(new Error('Could not create print document.'));
        return;
      }

      doc.open();
      doc.write(buildReportHtml(report));
      doc.close();

      const win = iframe.contentWindow!;

      const triggerPrint = async () => {
        try {
          // Wait for web fonts so the PDF uses Inter / JetBrains Mono, not a
          // fallback — but never hang if the font CDN is slow/offline.
          const fontsReady = win.document.fonts?.ready;
          if (fontsReady) {
            await Promise.race([fontsReady, new Promise(r => window.setTimeout(r, 1500))]);
          }
          win.focus();
          win.print();
          window.setTimeout(() => iframe.remove(), 1000);
          resolve();
        } catch (err) {
          iframe.remove();
          reject(err instanceof Error ? err : new Error('Print failed.'));
        }
      };

      if (doc.readyState === 'complete') {
        window.setTimeout(triggerPrint, 120);
      } else {
        iframe.addEventListener('load', () => window.setTimeout(triggerPrint, 120), { once: true });
      }
    } catch (err) {
      reject(err instanceof Error ? err : new Error('Export failed.'));
    }
  });
}

export type ShareResult = 'shared' | 'copied' | 'cancelled';

/**
 * Share the report verdict via the Web Share API, falling back to copying a
 * summary + repo link to the clipboard. Throws if neither path is available.
 */
export async function shareReport(report: ShipMateReport): Promise<ShareResult> {
  const verdict = toVerdict(report.ship_recommendation);
  const url = report.repo.html_url || window.location.href;
  const title = `ShipMate AI — ${report.repo.full_name}`;
  const text =
    `${report.repo.full_name} is ${verdict} (${report.readiness_score}/100 readiness).\n` +
    (report.key_blockers.length
      ? `Top blocker: ${report.key_blockers[0]}`
      : 'No critical blockers — cleared for deployment.');

  if (typeof navigator !== 'undefined' && typeof navigator.share === 'function') {
    try {
      await navigator.share({ title, text, url });
      return 'shared';
    } catch (err) {
      // User dismissed the share sheet — treat as a no-op, don't fall through
      // to clipboard (which would surprise them with a "copied" toast).
      if (err instanceof DOMException && err.name === 'AbortError') return 'cancelled';
      // Any other share failure: fall back to clipboard below.
    }
  }

  if (typeof navigator !== 'undefined' && navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(`${title}\n${text}\n${url}`);
    return 'copied';
  }

  throw new Error('Sharing is not supported in this browser.');
}
