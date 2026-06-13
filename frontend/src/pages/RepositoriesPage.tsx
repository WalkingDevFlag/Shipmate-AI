import { useState } from 'react';
import { motion } from 'framer-motion';
import { Search, Plus, BookOpen, AlertTriangle, Gauge } from 'lucide-react';
import { RepoCard } from '../components/ui/RepoCard';
import { EmptyState } from '../components/ui/EmptyState';
import type { GitHubRepo, ShipMateReport } from '../types';

interface Props {
  repos: GitHubRepo[];
  loadingRepos: boolean;
  selectedRepo: GitHubRepo | null;
  analyzing: boolean;
  report: ShipMateReport | null;
  onAnalyze: (r: GitHubRepo) => void;
  onViewReport: () => void;
  onConnect: () => void;
}

function RepoStat({ icon: Icon, tone, value, label }: { icon: React.ElementType; tone: string; value: number | string; label: string }) {
  const hexMap: Record<string, string> = { blue: '#3b82f6', emerald: '#10b981', purple: '#8b5cf6', red: '#ef4444' };
  const hex = hexMap[tone] ?? '#3b82f6';
  return (
    <div className="card" style={{ padding: 16 }}>
      <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
        <div style={{ width: 38, height: 38, borderRadius: 11, display: 'grid', placeItems: 'center', background: `${hex}18`, border: '1px solid var(--line-2)', color: hex }}>
          <Icon size={18} />
        </div>
        <div>
          <div style={{ fontSize: 22, fontWeight: 820, color: '#fff', lineHeight: 1 }}>{value}</div>
          <div className="muted" style={{ fontSize: 11.5, marginTop: 3 }}>{label}</div>
        </div>
      </div>
    </div>
  );
}

export function RepositoriesPage({ repos, loadingRepos, selectedRepo, analyzing, report, onAnalyze, onViewReport, onConnect }: Props) {
  const [query, setQuery] = useState('');
  const [lang, setLang]   = useState('All');
  const [sort, setSort]   = useState('name');

  if (!loadingRepos && repos.length === 0) {
    return (
      <div className="page-content" style={{ padding: '28px 32px', maxWidth: 1180, margin: '0 auto', width: '100%' }}>
        <EmptyState onConnect={onConnect} />
      </div>
    );
  }

  const analyzed = repos.filter(r => report && report.repo.name === r.name);
  const avgScore = report ? report.readiness_score : 0;
  const openRisks = report ? report.agents.guardrail.findings.filter(f => f.severity === 'critical' || f.severity === 'high').length : 0;
  const langs = ['All', ...Array.from(new Set(repos.map(r => r.language).filter(Boolean) as string[]))];

  let filtered = repos.filter(r =>
    (lang === 'All' || r.language === lang) &&
    (r.name.toLowerCase().includes(query.toLowerCase()) || r.full_name.toLowerCase().includes(query.toLowerCase())),
  );
  filtered = [...filtered].sort((a, b) => {
    if (sort === 'stars') return (b.stargazers_count ?? 0) - (a.stargazers_count ?? 0);
    return a.name.localeCompare(b.name);
  });

  return (
    <motion.div className="page-content" initial={{ opacity: 0, y: 14 }} animate={{ opacity: 1, y: 0 }} transition={{ duration: 0.55, ease: [0.2,0.7,0.2,1] }}
      style={{ padding: '28px 32px', maxWidth: 1180, margin: '0 auto', width: '100%' }}>
      {/* Header */}
      <div style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', flexWrap: 'wrap', gap: 12, marginBottom: 22 }}>
        <div>
          <h1 style={{ fontSize: 25, fontWeight: 840, margin: 0, letterSpacing: '-0.025em', color: '#fff' }}>Repositories</h1>
          <p className="muted" style={{ fontSize: 14, margin: '5px 0 0' }}>{repos.length} connected</p>
        </div>
        <button className="btn btn-light" onClick={onConnect}><Plus size={15} /> Connect Repository</button>
      </div>

      {/* Stats */}
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(4,1fr)', gap: 14, marginBottom: 18 }}>
        <RepoStat icon={BookOpen}      tone="blue"    value={repos.length}      label="Repositories" />
        <RepoStat icon={BookOpen}      tone="emerald" value={analyzed.length}   label="Analyzed" />
        <RepoStat icon={Gauge}         tone="purple"  value={avgScore || '—'}   label="Avg readiness" />
        <RepoStat icon={AlertTriangle} tone="red"     value={openRisks}         label="Open risks" />
      </div>

      {/* Search + filter */}
      <div className="card" style={{ display: 'flex', alignItems: 'center', gap: 12, flexWrap: 'wrap', padding: 12, marginBottom: 18 }}>
        <div style={{ position: 'relative', flex: 1, minWidth: 220 }}>
          <span style={{ position: 'absolute', left: 12, top: '50%', transform: 'translateY(-50%)', color: 'var(--ink-4)', pointerEvents: 'none' }}>
            <Search size={16} />
          </span>
          <input className="input" style={{ paddingLeft: 36 }} placeholder="Search repositories…"
            value={query} onChange={e => setQuery(e.target.value)} />
        </div>
        <select className="input" style={{ width: 'auto', minWidth: 130 }} value={lang} onChange={e => setLang(e.target.value)}>
          {langs.map(l => <option key={l} value={l}>{l === 'All' ? 'All languages' : l}</option>)}
        </select>
        <select className="input" style={{ width: 'auto', minWidth: 150 }} value={sort} onChange={e => setSort(e.target.value)}>
          <option value="name">Sort: Name</option>
          <option value="stars">Sort: Stars</option>
        </select>
      </div>

      {/* List */}
      {loadingRepos ? (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {[1,2,3].map(i => <div key={i} className="skel" style={{ height: 120, borderRadius: 18 }} />)}
        </div>
      ) : filtered.length === 0 ? (
        <div className="card" style={{ padding: 40, textAlign: 'center' }}>
          <span className="muted">No repositories match your filters.</span>
        </div>
      ) : (
        <div style={{ display: 'flex', flexDirection: 'column', gap: 12 }}>
          {filtered.map((r, i) => (
            <RepoCard
              key={r.id} repo={r} delay={i * 50}
              report={report && report.repo.name === r.name ? report : null}
              onAnalyze={onAnalyze}
              onReport={report && report.repo.name === r.name ? onViewReport : undefined}
              analyzing={analyzing} isSelected={selectedRepo?.id === r.id}
            />
          ))}
        </div>
      )}
    </motion.div>
  );
}
