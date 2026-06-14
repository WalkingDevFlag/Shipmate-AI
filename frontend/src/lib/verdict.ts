// Pure domain helpers for the readiness verdict — no React, so this is safe to
// import from both UI components and headless/string contexts (e.g. PDF export).

/** Map our 5-state backend recommendation to the simpler 3-state verdict. */
export function toVerdict(rec: string): string {
  if (rec === 'ready_to_ship' || rec === 'mostly_ready') return 'Ready';
  if (rec === 'not_ready' || rec === 'risky_release')    return 'Blocked';
  return 'Needs Fixes';
}

/**
 * Score → accent color. Mirrors `scoreColor` in lib/agents.ts exactly, but
 * lives here too so the PDF export module stays free of the lucide-react
 * import that agents.ts pulls in.
 */
export function scoreColor(score: number): string {
  if (score >= 80) return '#34d399'; // emerald — ready to ship
  if (score >= 60) return '#fbbf24'; // amber — needs review
  if (score >= 40) return '#fb923c'; // orange — risky
  return '#f87171';                  // red — not ready
}
