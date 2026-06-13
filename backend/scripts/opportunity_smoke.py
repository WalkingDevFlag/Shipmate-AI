"""
Opportunity Planner — offline smoke run against THIS repo.

The Phase-1A question is empirical: does ShipMate pick GENUINELY useful
self-improvement work, or generic mush? This script answers it WITHOUT GitHub —
it walks the local working tree, builds the same context dict
RepoAnalysisService produces, then runs the real pipeline:

    discover (LLM)  →  ground (deterministic)  →  suppress (journal)  →  rank

and prints the ranked opportunities with their grounding + scores so you can
judge quality by eye.

Usage (from backend/, with the venv active and ADA creds fresh):
    python -m scripts.opportunity_smoke               # this repo, 8 opps
    python -m scripts.opportunity_smoke --max 12
    python -m scripts.opportunity_smoke --root /path/to/other/repo
    python -m scripts.opportunity_smoke --include-ungrounded   # show drops too

Requires a live LLM provider (Bedrock via ADA, or Azure). With no provider it
prints ai_enhanced=False and an empty list — which is itself a useful signal.
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Dict, List

# Make `app` importable when run as a module from backend/.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.schemas.agent_schemas import BuildPlanResponse  # noqa: E402
from app.services.opportunity_service import OpportunityService  # noqa: E402

# Directories never worth walking for a code-improvement scan.
_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", "venv", "dist", "build",
    ".next", ".pytest_cache", "coverage", ".mypy_cache", ".ruff_cache",
    "build.log", ".idea", ".vscode",
}
# Extensions worth fetching contents for (source-ish files).
_CODE_EXTS = {
    ".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs", ".json", ".yml",
    ".yaml", ".toml", ".md", ".css", ".sh",
}
# Files we want the LLM to actually see (high-signal). Substring match on path.
_PREFER_TOKENS = (
    "main.py", "orchestrator", "/routes/", "/services/", "/agents/",
    "llm_service", "api.ts", "App.tsx", "/components/ui/", "/pages/",
    "repo_analysis", "coder_orchestrator",
)


def _build_local_context(root: Path, max_files: int = 60) -> Dict:
    """Walk the local tree and build a context dict shaped like
    RepoAnalysisService.build_context() output (offline equivalent).

    max_files is generous (60) so the FULL services/routes/agents surface is in
    the corpus — the capability digest + already-built detector need to SEE
    github_api_service.py / llm_provider.py / analysis.py to refute proposals
    that re-create their capabilities. The real /api/build/plan path achieves
    the same breadth via RepoAnalysisService.enrich_build_corpus."""
    file_tree: List[str] = []
    code_candidates: List[Path] = []

    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d not in _SKIP_DIRS]
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = full.relative_to(root).as_posix()
            file_tree.append(rel)
            if full.suffix.lower() in _CODE_EXTS:
                code_candidates.append(full)

    # Score candidates by how high-signal their path looks (same spirit as
    # llm_service._repo_code_blob's prefer scoring).
    def score(p: Path) -> int:
        rel = p.relative_to(root).as_posix().lower()
        s = 0
        for tok in _PREFER_TOKENS:
            if tok.lower() in rel:
                s += 20
        if rel.endswith((".py", ".ts", ".tsx")):
            s += 5
        # Prefer small/medium files (cheaper, more focused).
        try:
            sz = p.stat().st_size
            if sz < 16_000:
                s += 3
        except OSError:
            pass
        return s

    code_candidates.sort(key=score, reverse=True)

    key_files: Dict[str, str] = {}
    for p in code_candidates[:max_files]:
        rel = p.relative_to(root).as_posix()
        try:
            content = p.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        key_files[rel] = content            # path-keyed (what _repo_code_blob prefers)
        key_files[p.name] = content         # filename-keyed (RepoLens convenience)

    return {
        "repo_info": {
            "owner": "local",
            "name": root.name,
            "full_name": f"local/{root.name}",
            "description": "Local working-tree smoke run",
            "language": None,
        },
        "file_tree": file_tree,
        "key_files": key_files,
        "branch": "local",
        "pr_info": None,
        "feature_context": "",
    }


def _print_plan(plan: BuildPlanResponse, show_ungrounded: bool) -> None:
    bar = "═" * 78
    print(f"\n{bar}")
    print(f" OPPORTUNITY PLAN — {plan.owner}/{plan.repo} @ {plan.branch}")
    print(f" ai_enhanced={plan.ai_enhanced}  total_found={plan.total_found}  "
          f"grounded={plan.grounded_count}  verified={plan.verified_count}  "
          f"returned={len(plan.opportunities)}")
    print(bar)

    if not plan.opportunities:
        print("\n  (no opportunities returned)")
        if not plan.ai_enhanced:
            print("  → LLM provider unavailable. Check the Azure OpenAI "
                  "credentials (AZURE_OPENAI_ENDPOINT / AZURE_OPENAI_API_KEY) "
                  "and retry.")
        return

    for o in plan.opportunities:
        gflag = "" if o.grounded else "  ⚠ UNGROUNDED"
        jstate = f"  [journal:{o.journal_state}]" if o.journal_state else ""
        print(f"\n  ▸ {o.id}  [{o.category}]  value={o.value_score} "
              f"({o.priority})  effort={o.effort}/{o.estimated_days}d{gflag}{jstate}")
        print(f"    {o.title}")
        print(f"    impact: {o.impact}")
        if o.target_files:
            print(f"    files:  {', '.join(o.target_files)}")
        if o.evidence:
            print(f"    evidence:")
            for e in o.evidence:
                print(f"      - {e}")
        if o.suggested_approach:
            print(f"    approach:")
            for s in o.suggested_approach:
                print(f"      • {s}")

    # Quality summary the human can use to judge Phase-1A go/no-go.
    cats: Dict[str, int] = {}
    for o in plan.opportunities:
        cats[o.category] = cats.get(o.category, 0) + 1
    print(f"\n{bar}")
    print(f" CATEGORY MIX: " + "  ".join(f"{k}={v}" for k, v in sorted(cats.items())))
    print(f" GROUNDING:    {plan.grounded_count}/{plan.total_found} discovered "
          f"opportunities cited real files")
    print(bar + "\n")


def main() -> int:
    ap = argparse.ArgumentParser(description="Opportunity Planner smoke run (offline, local tree)")
    ap.add_argument("--root", default=None,
                    help="repo root to scan (default: the Shipmate-AI repo root)")
    ap.add_argument("--max", type=int, default=8, help="max opportunities")
    ap.add_argument("--include-ungrounded", action="store_true",
                    help="keep ungrounded opportunities (flagged) instead of dropping")
    args = ap.parse_args()

    if args.root:
        root = Path(args.root).resolve()
    else:
        # backend/scripts/ -> repo root is two levels up.
        root = Path(__file__).resolve().parent.parent.parent

    if not root.is_dir():
        print(f"error: {root} is not a directory", file=sys.stderr)
        return 2

    print(f"Scanning local tree: {root}")
    context = _build_local_context(root)
    print(f"  file_tree: {len(context['file_tree'])} paths · "
          f"key_files: {len([k for k in context['key_files'] if '/' in k])} fetched")

    plan = OpportunityService.build_plan(
        context, max_opportunities=args.max,
        include_ungrounded=args.include_ungrounded,
    )
    _print_plan(plan, args.include_ungrounded)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
