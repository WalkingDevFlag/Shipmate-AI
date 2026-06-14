"""reference_graph — a lightweight first-party import/symbol reference graph.

ShipMate has no cross-file dependency view: ast_lint reasons about ONE file at a
time, and repo_map lists paths + exported symbols with no EDGES between them.
For the research/improvement harness to surface dataflow-cleanup targets (dead
exports, cyclic imports, god-modules, orphan files), it needs the edges.

This builds that graph from the `key_files` corpus RepoIndexService already
fetched — no new GitHub I/O, no new dependency (stdlib `ast` via ast_lint's
existing collectors). Scope is FIRST-PARTY Python only (`app.*` / `backend.*` and
intra-package relative imports); third-party and non-Python files are nodes only
if something first-party imports them, otherwise ignored.

Per-module output (see ModuleNode): imports / imported_by (resolved to corpus
paths), exported_symbols, used_symbols (names other modules import FROM it), and
the derived signals the harness consumes: unreferenced_exports, is_orphan,
in_cycle, fan_in/fan_out.

Everything is fail-open: an unparseable file contributes no edges rather than
breaking the graph; an empty corpus yields an empty graph.
"""
from __future__ import annotations

import ast
import logging
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from app.services import ast_lint

logger = logging.getLogger("shipmate.reference_graph")

# A module is a "god module" candidate when an unusually large share of the
# graph depends on it. Tunable; expressed as a fan-in count threshold AND a
# share of the corpus, so it scales with repo size.
_GOD_MODULE_MIN_FANIN = 6


@dataclass
class ModuleNode:
    """One first-party module in the graph, keyed by its corpus path."""
    path: str
    module: str
    exported_symbols: Set[str] = field(default_factory=set)
    imports: Set[str] = field(default_factory=set)          # corpus paths this imports
    imported_by: Set[str] = field(default_factory=set)      # corpus paths importing this
    used_symbols: Set[str] = field(default_factory=set)     # of our defined exports, names others import
    parse_error: Optional[str] = None
    # importable surface (defined + re-exported); internal, used to match
    # used_symbols. Not part of the public dead-export signal.
    _importable: Set[str] = field(default_factory=set)

    @property
    def fan_in(self) -> int:
        return len(self.imported_by)

    @property
    def fan_out(self) -> int:
        return len(self.imports)

    @property
    def unreferenced_exports(self) -> Set[str]:
        """Exported names no OTHER first-party module imports. A cleanup signal,
        not a hard verdict — entry points, dynamic use, and test-only symbols can
        legitimately be unreferenced (the harness treats this as a candidate)."""
        return self.exported_symbols - self.used_symbols

    @property
    def is_orphan(self) -> bool:
        """Nothing imports it and it imports nothing first-party — a dangling
        island within the analyzed corpus."""
        return self.fan_in == 0 and self.fan_out == 0


@dataclass
class ReferenceGraph:
    nodes: Dict[str, ModuleNode] = field(default_factory=dict)  # path -> node
    cycles: List[List[str]] = field(default_factory=list)       # each a path list

    def god_modules(self) -> List[str]:
        """Paths whose fan-in is both absolutely high and a notable share of the
        corpus — the modules a change is most likely to ripple from."""
        if not self.nodes:
            return []
        share_cut = max(_GOD_MODULE_MIN_FANIN, len(self.nodes) // 4)
        return sorted(
            (p for p, n in self.nodes.items() if n.fan_in >= share_cut),
            key=lambda p: self.nodes[p].fan_in, reverse=True,
        )

    def orphans(self) -> List[str]:
        return sorted(p for p, n in self.nodes.items() if n.is_orphan)

    def unreferenced_exports(self) -> Dict[str, List[str]]:
        """path -> sorted dead-export names (only modules that have some)."""
        out: Dict[str, List[str]] = {}
        for p, n in self.nodes.items():
            dead = n.unreferenced_exports
            if dead:
                out[p] = sorted(dead)
        return out

    def to_summary(self) -> Dict[str, object]:
        """Compact, LLM-friendly digest the research pass grounds its findings
        on (so the model reasons about real edges, not guesses)."""
        return {
            "module_count": len(self.nodes),
            "edge_count": sum(n.fan_out for n in self.nodes.values()),
            "god_modules": [
                {"path": p, "fan_in": self.nodes[p].fan_in} for p in self.god_modules()
            ],
            "cycles": self.cycles,
            "orphans": self.orphans(),
            "unreferenced_exports": self.unreferenced_exports(),
        }


# ── all-scope import collection ──────────────────────────────────────────────

def _collect_all_imports(content: str):
    """Collect imports at EVERY scope (module level AND inside functions).

    ast_lint._collect_imports deliberately models module-level imports only —
    correct for import-fidelity linting (a function-body import can't break
    `import app.main`). But for the dependency GRAPH a lazy `from app.services
    import diff_apply` inside a method IS a real runtime edge; ignoring it makes
    the imported module look like an orphan. So the graph walks all scopes.

    Returns (plain_modules: set, module_imports: dict[mod -> set[names]]),
    matching the shape build_reference_graph already consumes. Empty on
    SyntaxError (the file becomes an edge-less node, flagged via parse_error)."""
    plain: Set[str] = set()
    module_imports: Dict[str, Set[str]] = {}
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return plain, module_imports
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                plain.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            mod = ("." * node.level) + (node.module or "")
            names = {a.name for a in node.names if a.name != "*"}
            module_imports.setdefault(mod, set()).update(names)
    return plain, module_imports


# ── defined (not imported) top-level symbols ─────────────────────────────────

def _defined_symbols(content: str) -> Set[str]:
    """Top-level names this module DEFINES (functions, classes, assignments) —
    EXCLUDING re-exported imports. ast_lint.module_exports intentionally counts
    `from x import y` as an export (it answers "what's importable from here", for
    the lint); for dead-export detection we want only what the module itself
    authors, so an imported-but-unused name isn't miscounted as a dead export."""
    try:
        tree = ast.parse(content or "")
    except SyntaxError:
        return set()
    names: Set[str] = set()
    for node in tree.body:  # top-level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            if not node.name.startswith("_"):  # private-by-convention: not API
                names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name) and not target.id.startswith("_"):
                    names.add(target.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name) and not node.target.id.startswith("_"):
                names.add(node.target.id)
        # Import / ImportFrom intentionally skipped — re-exports aren't "defined".
    return names


# ── module/path resolution ───────────────────────────────────────────────────

def _path_to_module(path: str) -> Optional[str]:
    """backend/app/main.py -> app.main; backend/foo.py -> foo. Reuses ast_lint's
    canonical mapping (one source of truth)."""
    return ast_lint._path_to_module(path)


def _candidate_paths_for_module(module: str) -> List[str]:
    return ast_lint._module_to_candidate_paths(module)


def _resolve_relative(module_with_dots: str, importer_module: str) -> Optional[str]:
    """Resolve a relative import (`.utils`, `..services.x`) against the importing
    module's dotted package. `.utils` from `app.foo.bar` -> `app.foo.utils`."""
    level = len(module_with_dots) - len(module_with_dots.lstrip("."))
    tail = module_with_dots[level:]
    parts = importer_module.split(".")
    # `.x` (level 1) is relative to the importer's PACKAGE (drop the module
    # name); each extra dot drops one more package level.
    base = parts[: max(0, len(parts) - level)]
    if not base:
        return None
    resolved = ".".join(base + ([tail] if tail else []))
    return resolved or None


def build_reference_graph(
    key_files: Dict[str, str], file_tree: Optional[List[str]] = None,
) -> ReferenceGraph:
    """Build the first-party import/symbol graph from the corpus.

    `key_files`: path -> source (what RepoIndexService fetched). Only `.py`
    files map to module nodes; only edges whose TARGET is also a node in the
    corpus are recorded (we can't analyze a body we didn't fetch).
    """
    graph = ReferenceGraph()
    # 1. Create a node per parseable first-party .py file + a module->path index.
    module_to_path: Dict[str, str] = {}
    for path, src in (key_files or {}).items():
        if not path.endswith(".py"):
            continue
        module = _path_to_module(path)
        if module is None:
            continue
        node = ModuleNode(path=path, module=module)
        # exported_symbols = names this module DEFINES (dead-export basis).
        node.exported_symbols = _defined_symbols(src or "")
        # importable surface (defined + re-exported) — what a `from m import X`
        # in another file could legitimately resolve to; used to mark used_symbols.
        node._importable = ast_lint.module_exports(src or "")
        graph.nodes[path] = node
        module_to_path[module] = path

    # 2. Resolve imports → edges (only when the target is a corpus node).
    for path, node in graph.nodes.items():
        src = key_files.get(path) or ""
        # Flag unparseable files (no edges) the same way as before.
        if ast_lint.syntax_error_of(src, path):
            node.parse_error = ast_lint.syntax_error_of(src, path)
            continue
        # All-scope collection: a lazy import inside a function IS a real graph
        # edge (see _collect_all_imports). `import a.b.c` + `from X import names`.
        plain_modules, module_imports = _collect_all_imports(src)
        targets: Dict[str, Set[str]] = {}
        for mod in plain_modules:
            targets.setdefault(mod, set())
        for mod, names in module_imports.items():
            resolved = _resolve_relative(mod, node.module) if mod.startswith(".") else mod
            if resolved:
                targets.setdefault(resolved, set()).update(names)

        for mod, names in targets.items():
            tgt_path = module_to_path.get(mod)
            if tgt_path is None:
                # Try candidate-path resolution for app.*/backend.* not keyed by
                # exact module (e.g. package __init__).
                for cand in _candidate_paths_for_module(mod):
                    if cand in graph.nodes:
                        tgt_path = cand
                        break

            # `from app.services import ast_lint` parses as module="app.services",
            # names={"ast_lint"} — i.e. the imported NAME is itself a sibling
            # MODULE, not a symbol of the package __init__. Without this, every
            # module imported that way looks like an orphan (0 importers) because
            # the only edge recorded points at the package node. For each name
            # that resolves to a real submodule path in the corpus, add a direct
            # module→module edge to it (and don't count it as a symbol of `mod`).
            symbol_names: Set[str] = set()
            for name in names:
                sub_path = module_to_path.get(f"{mod}.{name}")
                if sub_path is None:
                    for cand in _candidate_paths_for_module(f"{mod}.{name}"):
                        if cand in graph.nodes:
                            sub_path = cand
                            break
                if sub_path is not None and sub_path != path:
                    node.imports.add(sub_path)
                    graph.nodes[sub_path].imported_by.add(path)
                else:
                    symbol_names.add(name)  # a real symbol of `mod`, handled below

            if tgt_path is None or tgt_path == path:
                continue  # third-party, non-corpus, or self-import
            node.imports.add(tgt_path)
            graph.nodes[tgt_path].imported_by.add(path)
            # Record which of the target's DEFINED exports are actually imported
            # elsewhere (match against defined symbols so re-exports of a name
            # don't mask that the DEFINING module's symbol is unused). Only the
            # names that were NOT resolved to a submodule count as symbols here.
            graph.nodes[tgt_path].used_symbols.update(
                symbol_names & graph.nodes[tgt_path].exported_symbols
            )

    # 3. Detect import cycles (DFS over the path-edge graph).
    graph.cycles = _find_cycles(graph)
    for cyc in graph.cycles:
        for p in cyc:
            if p in graph.nodes:
                # mark via a sentinel attribute the summary already reads via cycles
                pass
    return graph


def _find_cycles(graph: ReferenceGraph) -> List[List[str]]:
    """Return distinct simple import cycles (each as a path list). Bounded DFS;
    de-dups rotations so A→B→A and B→A→B count once."""
    adj = {p: sorted(n.imports) for p, n in graph.nodes.items()}
    cycles: List[List[str]] = []
    seen_keys: Set[frozenset] = set()
    WHITE, GREY, BLACK = 0, 1, 2
    color: Dict[str, int] = {p: WHITE for p in adj}
    stack: List[str] = []

    def dfs(u: str) -> None:
        color[u] = GREY
        stack.append(u)
        for v in adj.get(u, []):
            if color.get(v) == GREY:
                # back-edge → cycle from v..u
                i = stack.index(v)
                cyc = stack[i:]
                key = frozenset(cyc)
                if len(cyc) >= 2 and key not in seen_keys:
                    seen_keys.add(key)
                    cycles.append(cyc[:])
            elif color.get(v) == WHITE:
                dfs(v)
        stack.pop()
        color[u] = BLACK

    for p in adj:
        if color[p] == WHITE:
            dfs(p)
    return cycles
