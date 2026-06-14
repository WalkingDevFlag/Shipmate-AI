"""
Repo map for the Coder brief — the prevention half of the import-hallucination
defense (ast_lint.detect_bad_imports is the cure).

Why this exists
---------------
A dogfood loop watched the Coder invent `from app.services.analysis_service
import ...` — a module that does not exist. The lint caught it after the fact
and rejected the patch, costing a full Bedrock round-trip. The cheaper fix is
to never let the model guess in the first place: show it, up front, the REAL
module layout and the REAL symbols each nearby module exports. With the true
namespace in front of it, the model has no reason to fabricate a plausible-
sounding sibling.

Two sections, both built from data the orchestrator ALREADY has (zero extra
GitHub I/O):

  1. Directory skeleton — an indented tree of the real files in the package(s)
     the target files live in, derived from `file_tree` (paths only). This is
     what kills the `analysis_service` class of hallucination: if it isn't in
     the tree, it doesn't exist, and the model can see that.

  2. Symbol index — for every first-party `.py` whose content we have on hand
     (`key_files` / the fetched `target_files`), its top-level exported symbols,
     extracted with `ast_lint.module_exports` — the SAME function the stale-
     named-import lint uses, so what the map advertises as importable can never
     disagree with what the lint will accept.

Pure (no IO), fail-open: any internal error returns "" rather than raising, so
a malformed tree can never break an actuate.
"""
from __future__ import annotations

import logging
from typing import Dict, List, Set

from app.services import ast_lint

logger = logging.getLogger("shipmate.repo_map")

# Directory name parts that are noise in a code map — vendored deps, build
# artifacts, caches, VCS metadata. A path containing any of these as a path
# segment is excluded from the skeleton.
_EXCLUDE_DIR_PARTS = frozenset({
    "__pycache__", "node_modules", ".git", ".venv", "venv", "env",
    "dist", "build", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    "site-packages", ".next", ".turbo", "coverage", ".cache", "htmlcov",
    ".idea", ".vscode", "__snapshots__",
})

# Extensions worth showing — source + the manifests/config Coder edits. Other
# files (images, lockfiles, .map) are dropped to keep the map signal-dense.
_KEEP_EXTS = (
    ".py", ".ts", ".tsx", ".js", ".jsx",
    ".yml", ".yaml", ".toml", ".cfg", ".ini", ".json", ".txt", ".md",
)

# Bounds so a huge monorepo can't blow the prompt budget. Backstopped by a
# hard char cap on the final string.
_MAX_SKELETON_PATHS = 220
_MAX_SYMBOL_MODULES = 40
_MAX_SYMBOLS_PER_MODULE = 24
_DEFAULT_MAX_CHARS = 6000


def _is_noise(path: str) -> bool:
    parts = path.split("/")
    if any(seg in _EXCLUDE_DIR_PARTS for seg in parts):
        return True
    name = parts[-1]
    # Keep extensionless dotfiles we care about (.gitignore), drop the rest.
    if name in (".gitignore", ".dockerignore", "Dockerfile", "Makefile"):
        return False
    return not name.endswith(_KEEP_EXTS)


def _focus_dirs(target_paths: List[str]) -> Set[str]:
    """Directories whose contents are worth showing: each target's own
    directory (its siblings — where a hallucinated import is most likely) plus
    the parent package directory (so editing a `services/` file also reveals
    the `agents/` / `api/` siblings it might legitimately import from).

    Returns dir prefixes WITHOUT a trailing slash; "" means repo root.
    """
    dirs: Set[str] = set()
    for p in target_paths:
        if not p:
            continue
        segments = p.split("/")
        parent = "/".join(segments[:-1])  # immediate dir
        dirs.add(parent)
        if len(segments) >= 3:
            dirs.add("/".join(segments[:-2]))  # package root
    return dirs


def _in_scope(path: str, focus_dirs: Set[str]) -> bool:
    """True if `path` lives under any focus dir (directory-boundary aware, so
    `app/serv` does not match `app/services/x.py`).

    The "" focus dir (a root-level target like requirements.txt / .gitignore /
    README.md) scopes to ROOT-LEVEL FILES ONLY — it must NOT match everything,
    or any root-level target would drag the entire repo (incl. an unrelated
    frontend tree) into the map and defeat the package-scoping that makes the
    map a useful anti-hallucination signal."""
    path_dir = "/".join(path.split("/")[:-1])
    for d in focus_dirs:
        if path_dir == d:
            return True  # exact dir match — covers d=="" ⇒ root-level files only
        if d and path_dir.startswith(d + "/"):
            return True  # under a focus subtree
    return False


def _render_skeleton(in_scope: List[str]) -> str:
    """Render a sorted path list as an indented directory tree."""
    # Nested dict tree: dict[name] -> subtree (dict) | None (file).
    root: Dict = {}
    for path in in_scope:
        node = root
        segs = path.split("/")
        for i, seg in enumerate(segs):
            is_file = i == len(segs) - 1
            if is_file:
                node.setdefault(seg, None)
            else:
                child = node.get(seg)
                if not isinstance(child, dict):
                    child = {}
                    node[seg] = child
                node = child

    lines: List[str] = []

    def walk(node: Dict, depth: int) -> None:
        # Dirs first, then files; each alpha-sorted — stable, readable output.
        items = sorted(
            node.items(),
            key=lambda kv: (kv[1] is None, kv[0].lower()),
        )
        for name, child in items:
            indent = "  " * depth
            if child is None:
                lines.append(f"{indent}{name}")
            else:
                lines.append(f"{indent}{name}/")
                walk(child, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def _render_symbols(key_files: Dict[str, str], target_paths: List[str]) -> str:
    """For each first-party `.py` we have content for, list its real top-level
    exports (via ast_lint.module_exports). Public names first; private kept but
    capped — they're still real, importable symbols."""
    lines: List[str] = []
    count = 0
    for path in sorted(key_files):
        if count >= _MAX_SYMBOL_MODULES:
            break
        content = key_files.get(path) or ""
        if not path.endswith(".py") or not content.strip():
            continue
        module = ast_lint._path_to_module(path)
        if module is None:
            continue
        # Advertise the IDIOMATIC import target. _path_to_module (shared with
        # lint) maps __init__.py to 'pkg.__init__'; that's a valid but ugly
        # import the model would copy. The package's own name is the canonical
        # target, and since module_exports folds in re-exports, the symbols we
        # list (e.g. names re-exported by the __init__) genuinely resolve via
        # `from pkg import X`. Display-only — lint still keys off the raw path.
        if module.endswith(".__init__"):
            module = module[: -len(".__init__")]
        elif module == "__init__":
            continue  # repo-root __init__ — no meaningful package name to show
        try:
            exports = ast_lint.module_exports(content)
        except Exception:  # pragma: no cover - module_exports is itself guarded
            continue
        if not exports:
            continue
        # Public symbols first (no leading underscore), then private, both
        # alpha-sorted, so the most-likely-imported names lead.
        ordered = sorted(exports, key=lambda n: (n.startswith("_"), n.lower()))
        shown = ordered[:_MAX_SYMBOLS_PER_MODULE]
        suffix = "" if len(ordered) <= _MAX_SYMBOLS_PER_MODULE else \
            f", … (+{len(ordered) - _MAX_SYMBOLS_PER_MODULE} more)"
        lines.append(f"- {module}: {', '.join(shown)}{suffix}")
        count += 1
    return "\n".join(lines)


def build_repo_map(
    file_tree: List[str],
    key_files: Dict[str, str],
    target_paths: List[str],
    max_chars: int = _DEFAULT_MAX_CHARS,
) -> str:
    """Build the Coder repo map. Returns "" when there's nothing useful to show
    (no tree and no parseable content) or on any internal error — the caller
    treats an empty map as "render no map section", so this is always safe.

    Args:
        file_tree:    every repo path (RepoLens / GitHub tree). Paths only.
        key_files:    path -> content for files we already fetched (the Coder's
                      target_files). Used for the symbol index.
        target_paths: the paths Coder is about to edit — used to scope the
                      skeleton to the relevant package(s).
        max_chars:    hard cap on the returned string (backstop on the per-
                      section bounds).
    """
    try:
        focus_dirs = _focus_dirs(target_paths)

        in_scope = [
            p for p in (file_tree or [])
            if p and not _is_noise(p) and _in_scope(p, focus_dirs)
        ]
        in_scope = sorted(set(in_scope))
        truncated_paths = False
        if len(in_scope) > _MAX_SKELETON_PATHS:
            in_scope = in_scope[:_MAX_SKELETON_PATHS]
            truncated_paths = True

        skeleton = _render_skeleton(in_scope) if in_scope else ""
        symbols = _render_symbols(key_files or {}, target_paths)

        if not skeleton and not symbols:
            return ""

        sections: List[str] = [
            "# Repo map (REAL modules + their exported symbols)",
            "Use ONLY modules and symbols that appear below for any cross-module "
            "import. If something you expected isn't here, it does NOT exist in "
            "this repo — do not import it.",
        ]
        if skeleton:
            note = " (truncated)" if truncated_paths else ""
            sections.append(f"\n## Files near the target(s){note}\n{skeleton}")
        if symbols:
            sections.append(
                "\n## Exported symbols (importable from these modules)\n" + symbols
            )

        out = "\n".join(sections)
        if len(out) > max_chars:
            out = out[:max_chars].rstrip() + "\n# … [repo map truncated by ShipMate]"
        return out
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("build_repo_map failed (%s) — returning empty map", e)
        return ""
