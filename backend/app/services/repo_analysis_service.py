"""
Repo Analysis Service

Fetches the repo file tree and a curated set of key files from GitHub,
then builds the context dict consumed by the ShipMate orchestrator.
"""

import asyncio
import logging
import os
from pathlib import Path
from typing import Any, Dict, List, Optional

import httpx

from .github_api_service import GitHubAPIService

logger = logging.getLogger("shipmate.repo_analysis")

# Files we always try to fetch (order = priority)
_KEY_FILE_NAMES = [
    "README.md", "readme.md", "README.rst", "README",
    "package.json",
    "requirements.txt", "requirements-dev.txt",
    "pyproject.toml", "setup.py",
    "Dockerfile",
    "docker-compose.yml", "docker-compose.yaml",
    ".env.example", ".env.sample",
    "tsconfig.json",
    "go.mod",
    "Cargo.toml",
    ".gitignore",
    "Makefile",
    "nginx.conf",
    "main.py", "app.py", "server.py",
    "index.ts", "index.js", "main.ts",
]

# Max files to fetch (GitHub API rate: 5000 req/hr for authenticated)
_MAX_KEY_FILES = 18

# Manifests/config that legitimately recur per-package in a monorepo — for these
# we fetch up to _MAX_MANIFEST_COPIES copies (shallowest first) so dependency
# extraction sees backend/ AND frontend/ AND services/* deps, not just one.
# Everything else (README, Makefile, .gitignore, entry files) takes one copy.
_MULTI_COPY_MANIFESTS = frozenset({
    "package.json", "requirements.txt", "requirements-dev.txt",
    "pyproject.toml", "setup.py", "go.mod", "Cargo.toml", "tsconfig.json",
})
_MAX_MANIFEST_COPIES = 4

# Overall ceiling on the parallel key-file fetch, so a few hung connections
# can't stall the whole analyze request (the gather had no outer timeout).
_FILES_FETCH_TIMEOUT_S = float(os.getenv("SHIPMATE_FILES_FETCH_TIMEOUT_S", "60"))


class RepoAnalysisService:

    @classmethod
    async def build_context(
        cls,
        token: str,
        owner: str,
        repo: str,
        branch: str = "main",
        pr_number: Optional[int] = None,
        feature_context: str = "",
    ) -> Dict[str, Any]:
        """
        Fetches repo data from GitHub and returns the context dict
        expected by ShipMateOrchestrator.run().
        """
        # Fetch repo info and file tree in parallel
        repo_info, file_tree = await asyncio.gather(
            GitHubAPIService.get_repo_info(token, owner, repo),
            GitHubAPIService.get_file_tree(token, owner, repo, branch),
        )

        # Determine which files to fetch
        files_to_fetch = cls._select_key_files(file_tree)

        # Fetch key file contents in parallel (bounded concurrency), pinned to
        # the analyzed branch so the corpus reflects THIS branch, not default.
        key_files = await cls._fetch_files(token, owner, repo, files_to_fetch, ref=branch)

        # Optional PR context. A missing PR (404) is an expected, benign case —
        # we proceed with pr_info=None. But a transient/server error (429/5xx,
        # network) should NOT be silently swallowed: it means the PR context the
        # caller asked for is missing for a fixable reason. We surface it via a
        # `warnings` list in the context so the orchestrator/route can tell the
        # user "PR context unavailable" instead of degrading invisibly.
        pr_info = None
        warnings: List[str] = []
        if pr_number:
            try:
                pr_info = await GitHubAPIService.get_pr_info(token, owner, repo, pr_number)
            except httpx.HTTPStatusError as e:
                status = e.response.status_code if e.response is not None else None
                if status == 404:
                    # PR genuinely not found — expected, proceed without it.
                    pass
                else:
                    msg = f"Could not fetch PR #{pr_number} context (HTTP {status}); analysis proceeds without PR diff."
                    warnings.append(msg)
                    logger.warning(msg)
            except Exception as e:
                msg = f"Could not fetch PR #{pr_number} context ({type(e).__name__}); analysis proceeds without PR diff."
                warnings.append(msg)
                logger.warning(msg)

        return {
            "repo_info": repo_info,
            "file_tree": file_tree,
            "key_files": key_files,
            "branch": branch,
            "pr_info": pr_info,
            "feature_context": feature_context,
            "warnings": warnings,
        }

    # Source files worth fetching for the BUILD path specifically. The default
    # key-file set (~18 config + entry files) is enough for analyze, but the
    # Opportunity Planner needs to SEE the route/service/agent code so it (a)
    # doesn't propose adding capabilities that already exist there, and (b) the
    # already-built detector + LLM critic have the proof-of-existence in hand.
    # Tokens that mark a path as high-signal app code.
    _BUILD_SOURCE_TOKENS = (
        "/routes/", "/api/", "/services/", "/agents/", "/orchestrator/",
        "/schemas/", "/components/", "/pages/", "/hooks/", "/lib/",
        "main.py", "app.py", "api.ts", "app.tsx",
    )
    _BUILD_SOURCE_EXTS = (".py", ".ts", ".tsx", ".js", ".jsx", ".go", ".rs")
    _BUILD_SKIP_TOKENS = (
        "node_modules/", "/dist/", "/build/", "__pycache__/", "/.venv/",
        "/venv/", "/tests/", "/test/", "/__tests__/", ".test.", ".spec.",
        "/migrations/", ".min.", "dist-demo/",
    )

    @classmethod
    async def enrich_build_corpus(
        cls, token: str, owner: str, repo: str, context: Dict[str, Any],
        max_source_files: int = 40,
    ) -> Dict[str, Any]:
        """Fetch a broad set of SOURCE files (routes/services/agents/components)
        into context['key_files'] so the opportunity pipeline can see what the
        codebase already does. Mutates and returns `context`. Best-effort: a
        fetch failure just means fewer files, never an error. Files already
        present in key_files are skipped (no double fetch)."""
        tree: List[str] = context.get("file_tree") or []
        already = set((context.get("key_files") or {}).keys())

        def _is_source(p: str) -> bool:
            lp = p.lower()
            if any(skip in lp for skip in cls._BUILD_SKIP_TOKENS):
                return False
            if not lp.endswith(cls._BUILD_SOURCE_EXTS):
                return False
            return any(tok in lp for tok in cls._BUILD_SOURCE_TOKENS)

        # Rank by signal: route/service/agent files first, then by short path
        # depth (top-level app code over deeply nested helpers).
        def _rank(p: str) -> int:
            lp = p.lower()
            s = 0
            for w, toks in (
                (40, ("/routes/", "/api/")),
                (30, ("/services/", "/orchestrator/", "/agents/")),
                (20, ("/schemas/", "/pages/", "/components/", "/hooks/", "/lib/")),
            ):
                if any(t in lp for t in toks):
                    s += w
            s -= lp.count("/")  # prefer shallower paths slightly
            return s

        candidates = sorted(
            (p for p in tree if _is_source(p) and p not in already),
            key=_rank, reverse=True,
        )[:max_source_files]

        if not candidates:
            return context

        # Pin to the analyzed branch (from context) — same staleness fix as
        # build_context; without it the enriched source is default-branch.
        branch = context.get("branch") or "main"
        fetched = await cls._fetch_files(token, owner, repo, candidates, ref=branch)
        key_files = context.get("key_files") or {}
        key_files.update(fetched)
        context["key_files"] = key_files
        return context

    @classmethod
    def _select_key_files(cls, tree: List[str]) -> List[str]:
        """Pick files to fetch: prioritized list + first workflow yaml.

        Matches each name in _KEY_FILE_NAMES against the BASENAME of every tree
        path — NOT exact equality. The tree holds full paths (e.g.
        'backend/requirements.txt'), so the old `name in tree_set` check never
        matched a manifest that lived in a subdirectory: in any monorepo /
        frontend+backend split the manifests were never fetched, dependency
        extraction was a no-op, and every downstream agent inherited an empty
        tech-stack picture. Manifests can legitimately appear in several
        directories, so for those we collect MULTIPLE copies (shallowest first);
        single-instance files (README, Makefile, …) take just the shallowest."""
        selected: List[str] = []

        # Index tree paths by basename, shallowest path first (fewest slashes),
        # so the root/most-canonical copy leads.
        by_name: Dict[str, List[str]] = {}
        for p in tree:
            by_name.setdefault(Path(p).name, []).append(p)
        for name in by_name:
            by_name[name].sort(key=lambda p: (p.count("/"), p))

        # Manifests/configs that genuinely recur per-package in a monorepo — take
        # up to _MAX_MANIFEST_COPIES of each so multi-directory deps are seen.
        for name in _KEY_FILE_NAMES:
            matches = by_name.get(name, [])
            if not matches:
                continue
            take = _MAX_MANIFEST_COPIES if name in _MULTI_COPY_MANIFESTS else 1
            for path in matches[:take]:
                if path not in selected:
                    selected.append(path)
            if len(selected) >= _MAX_KEY_FILES:
                break

        # Add first GitHub Actions workflow if present
        workflows = sorted(
            (f for f in tree if ".github/workflows" in f and f.endswith((".yml", ".yaml"))),
            key=lambda p: (p.count("/"), p),
        )
        if workflows and workflows[0] not in selected:
            selected.append(workflows[0])

        # Add first entry file not yet in list
        ENTRY_NAMES = {"main.py", "app.py", "server.py", "index.ts", "index.js", "main.ts", "main.js"}
        for f in sorted(tree, key=lambda p: (p.count("/"), p)):
            if Path(f).name in ENTRY_NAMES and f not in selected:
                selected.append(f)
                break

        return selected[:_MAX_KEY_FILES]

    @classmethod
    async def _fetch_files(
        cls, token: str, owner: str, repo: str, paths: List[str],
        ref: Optional[str] = None,
    ) -> Dict[str, str]:
        """Fetch files with bounded concurrency (max 6 parallel requests).

        `ref` pins the fetch to a specific branch/sha. CRITICAL: without it,
        GitHub serves the repo's DEFAULT branch, so analyzing a feature branch
        returned stale default-branch content — the agents then flagged issues
        already fixed on the analyzed branch (and the deterministic
        already-resolved gate couldn't find its proof files because they didn't
        exist on default yet). Pinning to the analyzed branch fixes that."""
        sem = asyncio.Semaphore(6)

        async def fetch_one(path: str):
            async with sem:
                content = await GitHubAPIService.get_file_content(
                    token, owner, repo, path, ref=ref,
                )
                return path, content

        # Overall timeout so a few slow/hung fetches can't stall context-build
        # (and the whole analyze request) indefinitely. On timeout we keep
        # whatever returned and proceed with a partial corpus.
        try:
            results = await asyncio.wait_for(
                asyncio.gather(*[fetch_one(p) for p in paths], return_exceptions=True),
                timeout=_FILES_FETCH_TIMEOUT_S,
            )
        except asyncio.TimeoutError:
            logger.warning(
                "_fetch_files timed out after %ss for %s/%s (%d paths) — "
                "proceeding with whatever was fetched",
                _FILES_FETCH_TIMEOUT_S, owner, repo, len(paths),
            )
            return {}

        key_files: Dict[str, str] = {}
        for result in results:
            if isinstance(result, Exception):
                continue
            path, content = result
            if content is not None:
                key_files[Path(path).name] = content  # store by filename for easy agent access
                key_files[path] = content              # also store by full path

        return key_files
