import { useState, useRef, useEffect } from 'react';
import { LayoutDashboard, FolderGit2, Activity, FileBarChart2, Hammer, LogOut, ChevronDown, Check, Lock, BookOpen, X, Sparkles, Bell, Zap } from 'lucide-react';
import { Wordmark } from '../ui/LogoMark';
import { GitHubIcon } from '../ui/GitHubIcon';
import { BranchPicker } from '../ui/BranchPicker';
import { LANG_COLORS } from '../../lib/agents';
import type { GitHubUser, GitHubRepo } from '../../types';

export type Page = 'dashboard' | 'repos' | 'analysis' | 'build' | 'reports';

const NAV_ITEMS: { id: Page; label: string; Icon: React.ElementType }[] = [
  { id: 'dashboard', label: 'Dashboard',    Icon: LayoutDashboard },
  { id: 'repos',     label: 'Repositories', Icon: FolderGit2 },
  { id: 'analysis',  label: 'Analysis',     Icon: Activity },
  { id: 'build',     label: 'Build',        Icon: Hammer },
  { id: 'reports',   label: 'Reports',      Icon: FileBarChart2 },
];

/* ---------- Repo switcher ---------- */
function RepoSwitcher({ repos, current, onSelect }: { repos: GitHubRepo[]; current: GitHubRepo | null; onSelect: (r: GitHubRepo) => void }) {
  const [open, setOpen] = useState(false);
  const ref = useRef<HTMLDivElement>(null);

  useEffect(() => {
    const h = (e: MouseEvent) => { if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false); };
    document.addEventListener('mousedown', h);
    return () => document.removeEventListener('mousedown', h);
  }, []);

  if (!current) return null;

  return (
    <div ref={ref} style={{ position: 'relative', margin: '0 0 4px' }}>
      <button
        onClick={() => setOpen(o => !o)}
        style={{
          width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '10px 12px', borderRadius: 12, border: '1px solid var(--line)',
          background: 'rgba(255,255,255,0.025)', cursor: 'pointer', transition: 'border-color .2s',
        }}
        onMouseEnter={e => (e.currentTarget.style.borderColor = 'var(--line-2)')}
        onMouseLeave={e => (e.currentTarget.style.borderColor = 'var(--line)')}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
          <div style={{ width: 26, height: 26, borderRadius: 8, background: 'linear-gradient(135deg,#2563eb,#0e7490)', display: 'grid', placeItems: 'center', flexShrink: 0 }}>
            {current.private ? <Lock size={12} style={{ color: '#fff' }} /> : <BookOpen size={12} style={{ color: '#fff' }} />}
          </div>
          <div style={{ minWidth: 0, textAlign: 'left' }}>
            <div className="mono" style={{ fontSize: 12.5, fontWeight: 650, color: '#fff', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{current.name}</div>
            <div style={{ fontSize: 10, color: 'var(--ink-3)' }}>{current.owner.login}</div>
          </div>
        </div>
        <ChevronDown size={14} style={{ color: 'var(--ink-3)', flexShrink: 0, transform: open ? 'rotate(180deg)' : 'none', transition: 'transform .2s' }} />
      </button>

      {open && (
        <div className="card anim-scale" style={{ position: 'absolute', top: 'calc(100% + 6px)', left: 0, right: 0, zIndex: 60, padding: 6, boxShadow: 'var(--shadow)' }}>
          <div className="eyebrow" style={{ padding: '8px 10px 6px' }}>Switch repository</div>
          {repos.slice(0, 8).map(r => (
            <button key={r.id} onClick={() => { onSelect(r); setOpen(false); }}
              style={{ width: '100%', display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 10px', borderRadius: 8, background: 'none', border: 'none', cursor: 'pointer', color: 'inherit' }}
              onMouseEnter={e => (e.currentTarget.style.background = 'rgba(255,255,255,0.04)')}
              onMouseLeave={e => (e.currentTarget.style.background = 'none')}
            >
              <div style={{ display: 'flex', alignItems: 'center', gap: 8, minWidth: 0 }}>
                <span className="dot" style={{ background: LANG_COLORS[r.language ?? ''] ?? '#64748b', flexShrink: 0 }} />
                <span className="mono" style={{ fontSize: 12.5, color: r.id === current.id ? '#fff' : 'var(--ink-2)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.name}</span>
              </div>
              {r.id === current.id && <Check size={14} style={{ color: 'var(--blue-2)', flexShrink: 0 }} />}
            </button>
          ))}
        </div>
      )}
    </div>
  );
}

/* ---------- Main sidebar ---------- */
interface AppSidebarProps {
  activePage: Page;
  onNavigate: (p: Page) => void;
  user: GitHubUser | null;
  onLogout: () => void;
  repos?: GitHubRepo[];
  selectedRepo?: GitHubRepo | null;
  onSelectRepo?: (r: GitHubRepo) => void;
  hasReport?: boolean;
  // Mobile drawer
  mobileOpen?: boolean;
  onMobileClose?: () => void;
  isMobile?: boolean;
  // Mobile: also expose branch picker + actions inside the drawer
  selectedBranch?: string;
  onBranchChange?: (b: string) => void;
  accessToken?: string | null;
  analyzing?: boolean;
  onAnalyze?: () => void;
  onAutoFix?: () => void;
}

export function AppSidebar({
  activePage, onNavigate, user, onLogout,
  repos = [], selectedRepo = null, onSelectRepo, hasReport = false,
  mobileOpen = false, onMobileClose, isMobile = false,
  selectedBranch, onBranchChange, accessToken,
  analyzing = false, onAnalyze, onAutoFix,
}: AppSidebarProps) {
  const initials = user ? (user.name ?? user.login).split(' ').map(w => w[0]).join('').slice(0, 2).toUpperCase() : 'U';

  // On mobile the sidebar is a fixed slide-in drawer (off-canvas by default);
  // on desktop it's the sticky 248px column.
  const sidebarStyle: React.CSSProperties = isMobile ? {
    position: 'fixed', left: 0, top: 0, width: 280, height: '100vh', zIndex: 100,
    transform: mobileOpen ? 'translateX(0)' : 'translateX(-100%)',
    transition: 'transform 0.25s cubic-bezier(0.4, 0, 0.2, 1)',
    background: 'linear-gradient(180deg, var(--sidebar-top), var(--sidebar-bot))',
    borderRight: '1px solid var(--line)', display: 'flex', flexDirection: 'column', padding: 16,
    overflowY: 'auto',
  } : {
    width: 248, flexShrink: 0, height: '100vh', position: 'sticky', top: 0,
    background: 'linear-gradient(180deg, var(--sidebar-top), var(--sidebar-bot))',
    borderRight: '1px solid var(--line)', display: 'flex', flexDirection: 'column', padding: 16,
    overflowY: 'auto',
  };

  return (
    <aside style={sidebarStyle}>
      {/* Brand + (mobile) close button */}
      <div style={{ padding: '6px 6px 14px', display: 'flex', alignItems: 'center', justifyContent: 'space-between' }}>
        <Wordmark size={16.5} markSize={32} />
        {isMobile && (
          <button
            onClick={onMobileClose}
            style={{ padding: 6, borderRadius: 8, background: 'rgba(255,255,255,0.05)', border: '1px solid var(--line)', color: 'var(--ink-3)', cursor: 'pointer', display: 'flex', alignItems: 'center' }}
            aria-label="Close menu"
          >
            <X size={16} />
          </button>
        )}
      </div>

      {/* Repo switcher */}
      {selectedRepo && onSelectRepo && (
        <RepoSwitcher repos={repos} current={selectedRepo} onSelect={onSelectRepo} />
      )}

      {/* Connected to GitHub strip */}
      <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '12px 2px 8px', padding: '8px 10px', borderRadius: 10, background: 'rgba(16,185,129,0.08)', border: '1px solid rgba(16,185,129,0.2)' }}>
        <GitHubIcon size={15} style={{ color: '#fff' }} />
        <span style={{ fontSize: 11.5, color: '#a7f3d0', fontWeight: 600 }}>Connected to GitHub</span>
        <span className="dot dot-pulse" style={{ background: '#34d399', marginLeft: 'auto' }} />
      </div>

      {/* Mobile-only: branch picker + action buttons (topbar controls move here) */}
      {isMobile && selectedRepo && (
        <div style={{ margin: '4px 2px 12px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {selectedBranch && onBranchChange && (
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              <span style={{ fontSize: 11, color: 'var(--ink-3)', fontWeight: 600, minWidth: 48 }}>Branch</span>
              <BranchPicker
                repoFullName={selectedRepo.full_name}
                defaultBranch={selectedRepo.default_branch}
                selected={selectedBranch}
                onChange={onBranchChange}
                accessToken={accessToken ?? null}
                disabled={analyzing}
              />
            </div>
          )}
          <div style={{ display: 'flex', gap: 8 }}>
            <button
              className="btn btn-primary btn-sm"
              onClick={() => { onAnalyze?.(); onMobileClose?.(); }}
              disabled={analyzing || !selectedRepo}
              style={{ flex: 1, justifyContent: 'center' }}
            >
              <Zap size={14} /> {analyzing ? 'Running…' : 'Run Analysis'}
            </button>
            <button
              className="btn btn-secondary btn-sm"
              onClick={() => { onAutoFix?.(); onMobileClose?.(); }}
              disabled={analyzing || !selectedRepo}
              title="Auto-fix loop"
            >
              <Sparkles size={14} />
            </button>
          </div>
        </div>
      )}

      {/* Mobile-only: user status row */}
      {isMobile && user && (
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, margin: '0 2px 12px', padding: '8px 10px', borderRadius: 10, background: 'rgba(255,255,255,0.03)', border: '1px solid var(--line)' }}>
          {user.avatar_url
            ? <img src={user.avatar_url} alt="" style={{ width: 28, height: 28, borderRadius: 8, objectFit: 'cover', flexShrink: 0 }} />
            : <div style={{ width: 28, height: 28, borderRadius: 8, background: 'linear-gradient(135deg,#8b5cf6,#3b82f6)', display: 'grid', placeItems: 'center', fontSize: 10, fontWeight: 700, color: '#fff', flexShrink: 0 }}>
                {(user.name ?? user.login).slice(0, 2).toUpperCase()}
              </div>
          }
          <div style={{ minWidth: 0, flex: 1 }}>
            <div style={{ fontSize: 12, fontWeight: 650, color: '#fff', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{user.name || user.login}</div>
            <div className="mono" style={{ fontSize: 10, color: 'var(--ink-3)' }}>@{user.login}</div>
          </div>
          <Bell size={15} style={{ color: 'var(--ink-3)', flexShrink: 0 }} />
        </div>
      )}

      {/* Nav label */}
      <div className="eyebrow" style={{ padding: '0 8px 8px' }}>Navigate</div>

      {/* Nav items */}
      <nav style={{ display: 'flex', flexDirection: 'column', gap: 2 }}>
        {NAV_ITEMS.map(item => {
          const disabled = item.id === 'reports' && !hasReport;
          const isActive  = activePage === item.id;
          return (
            <button
              key={item.id}
              className={`nav-item ${isActive ? 'active' : ''} ${disabled ? 'disabled' : ''}`}
              onClick={() => !disabled && onNavigate(item.id)}
              aria-disabled={disabled}
            >
              <item.Icon size={17} className="nav-ico" />
              {item.label}
              {disabled && <span className="kbd" style={{ marginLeft: 'auto', fontSize: 9 }}>locked</span>}
              {isActive && <span className="dot" style={{ background: 'var(--blue-2)', marginLeft: 'auto', boxShadow: '0 0 8px var(--blue-2)' }} />}
            </button>
          );
        })}
      </nav>

      {/* Bottom section */}
      <div style={{ marginTop: 'auto', paddingTop: 16 }}>
        {/* Systems status */}
        <div style={{ display: 'flex', alignItems: 'center', gap: 8, padding: '9px 11px', borderRadius: 11, background: 'rgba(16,185,129,0.06)', border: '1px solid rgba(16,185,129,0.18)', marginBottom: 12 }}>
          <span className="dot dot-pulse" style={{ background: '#34d399' }} />
          <span style={{ fontSize: 11.5, fontWeight: 600, color: '#a7f3d0' }}>All Systems Operational</span>
        </div>

        {/* User row */}
        {user && (
          <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '8px 6px' }}>
            <div style={{ display: 'flex', alignItems: 'center', gap: 8 }}>
              {user.avatar_url
                ? <img src={user.avatar_url} alt="" style={{ width: 32, height: 32, borderRadius: 9, objectFit: 'cover' }} />
                : <div style={{ width: 32, height: 32, borderRadius: 9, background: 'linear-gradient(135deg,#8b5cf6,#3b82f6)', display: 'grid', placeItems: 'center', fontSize: 12, fontWeight: 700, color: '#fff' }}>{initials}</div>
              }
              <div>
                <div style={{ fontSize: 12.5, fontWeight: 650, color: '#fff' }}>{user.name || user.login}</div>
                <div className="mono" style={{ fontSize: 10.5, color: 'var(--ink-3)' }}>@{user.login}</div>
              </div>
            </div>
            <button
              onClick={onLogout}
              style={{ padding: 7, borderRadius: 8, background: 'none', border: 'none', color: 'var(--ink-3)', cursor: 'pointer', transition: 'color .15s' }}
              onMouseEnter={e => (e.currentTarget.style.color = '#f87171')}
              onMouseLeave={e => (e.currentTarget.style.color = 'var(--ink-3)')}
              title="Sign out"
            >
              <LogOut size={15} />
            </button>
          </div>
        )}
      </div>
    </aside>
  );
}
