"""
AST-based lint for Coder output — replaces the regex hallucination detectors
that lived in coder_orchestrator.py.

Why AST instead of regex:
  The regex `_python_imports` / `_detect_hallucinated_*` had two failure modes
  the loop surfaced over 24 rounds:
    1. False positives on aliased / conditional imports. `from .utils import
       helper as _h`, `if TYPE_CHECKING: from foo import Bar`, and
       `try: import ujson as json / except ImportError: import json` all got
       flagged as hallucinations even though they're legitimate.
    2. Missed multi-line and nested forms. The parenthesized-import regex was
       bolted on after the flat one missed it; comments inside import blocks
       confused both.

  Parsing with `ast` gets all of this for free: real import nodes, real
  symbol definitions, real scope. We only inspect MODULE-LEVEL imports
  (the ones that run at import time and can break `import app.main`),
  and we explicitly skip imports guarded by `if TYPE_CHECKING:` or
  `try/except ImportError`.

Public API (called by coder_orchestrator._lint_coder_output):
  detect_bad_imports(new_content, original_content, file_tree) -> List[str]
      First-party (app./backend.) imports in new_content that aren't in the
      original AND don't map to a real path in the repo tree.

  detect_stale_named_imports(new_content, target_files, coder_files) -> List[str]
      `from X import a` where X is a module being shipped in THIS patch (or
      already in target_files) but `a` isn't defined in X's final content.

  syntax_error_of(content, path) -> Optional[str]
      Returns a human string if `content` doesn't parse, else None. The
      orchestrator treats unparseable Coder output as a fatal lint issue —
      no point shipping a .py that won't even import.

  module_exports(content) -> Set[str]
      Top-level symbols a module exposes (functions, classes, module-level
      assignments, re-exports). The single source of truth shared by the
      stale-named-import detector (cure) AND repo_map (prevention) — so what
      repo_map advertises as importable can never disagree with what lint
      will accept.

All functions are pure (no IO) so they're trivially unit-testable.
"""
from __future__ import annotations

import ast
import logging
from typing import Any, Dict, List, Optional, Set, Tuple

logger = logging.getLogger("shipmate.ast_lint")

_FIRST_PARTY_PREFIXES = ("app.", "backend.")


# ── Import extraction ───────────────────────────────────────────────────────

class _ImportCollector(ast.NodeVisitor):
    """Walks a module and collects MODULE-LEVEL imports, skipping those
    guarded by `if TYPE_CHECKING:` or `try/except ImportError`.

    We only care about top-level (module-scope) imports because those run at
    import time — an import inside a function body can't break
    `import app.main`. The visitor tracks a `_depth` counter: imports are
    collected only at depth 0 (module body) unless we've explicitly
    descended into a TYPE_CHECKING / try-ImportError block, which we skip
    entirely.
    """

    def __init__(self) -> None:
        # module -> set of imported names (empty set = `import module`)
        self.module_imports: Dict[str, Set[str]] = {}
        # plain `import a.b.c` modules
        self.plain_modules: Set[str] = set()
        self._scope_depth = 0  # 0 == module level

    # --- skip guarded blocks ------------------------------------------------

    def visit_If(self, node: ast.If) -> None:
        if self._is_type_checking_test(node.test):
            # Skip the body (TYPE_CHECKING imports never run at runtime), but
            # still visit the else-branch which DOES run.
            for stmt in node.orelse:
                self.visit(stmt)
            return
        self.generic_visit(node)

    def visit_Try(self, node: ast.Try) -> None:
        # If any handler catches ImportError/ModuleNotFoundError, the import
        # in the try body is an intentional optional dependency — skip the
        # whole construct (body + handlers).
        if self._has_import_error_handler(node):
            return
        self.generic_visit(node)

    # --- only collect at module scope --------------------------------------

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._scope_depth += 1
        self.generic_visit(node)
        self._scope_depth -= 1

    # --- collect imports ----------------------------------------------------

    def visit_Import(self, node: ast.Import) -> None:
        if self._scope_depth != 0:
            return
        for alias in node.names:
            self.plain_modules.add(alias.name)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self._scope_depth != 0:
            return
        # Relative imports (`.utils`, `..foo`) — node.level > 0. We resolve
        # those against the file's package below; for now record with the
        # leading dots so the caller can decide.
        module = ("." * node.level) + (node.module or "")
        names = {alias.name for alias in node.names if alias.name != "*"}
        self.module_imports.setdefault(module, set()).update(names)

    # --- helpers ------------------------------------------------------------

    @staticmethod
    def _is_type_checking_test(test: ast.expr) -> bool:
        """True for `if TYPE_CHECKING:` or `if typing.TYPE_CHECKING:`."""
        if isinstance(test, ast.Name) and test.id == "TYPE_CHECKING":
            return True
        if isinstance(test, ast.Attribute) and test.attr == "TYPE_CHECKING":
            return True
        return False

    @staticmethod
    def _has_import_error_handler(node: ast.Try) -> bool:
        for handler in node.handlers:
            exc = handler.type
            if exc is None:
                # bare `except:` — treat as catching import errors too
                return True
            names = []
            if isinstance(exc, ast.Name):
                names = [exc.id]
            elif isinstance(exc, ast.Tuple):
                names = [e.id for e in exc.elts if isinstance(e, ast.Name)]
            if any(n in ("ImportError", "ModuleNotFoundError") for n in names):
                return True
        return False


def _collect_imports(content: str) -> Tuple[_ImportCollector, Optional[str]]:
    """Parse `content` and return (collector, syntax_error_or_None)."""
    try:
        tree = ast.parse(content)
    except SyntaxError as e:
        return _ImportCollector(), f"line {e.lineno}: {e.msg}"
    collector = _ImportCollector()
    collector.visit(tree)
    return collector, None


# ── Symbol definitions (for stale-named-import detection) ───────────────────

def module_exports(content: str) -> Set[str]:
    """Names a module exposes at top level: functions, classes, and
    module-level assignments (incl. annotated assignments and `__all__`-style
    tuples). Used to verify `from module import name` actually resolves AND to
    advertise a module's importable symbols in the Coder repo map — one source
    of truth so prevention (repo_map) and cure (stale-import lint) never
    disagree on what a module exports.

    Returns an empty set on SyntaxError — caller will have already flagged
    the syntax error separately, and an empty set means we won't add MORE
    noise about missing symbols on top of the parse failure.
    """
    try:
        tree = ast.parse(content)
    except SyntaxError:
        return set()

    names: Set[str] = set()
    for node in tree.body:  # top-level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                _collect_assign_targets(target, names)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            # Re-exported names: `from .x import y` makes `y` importable from here.
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
    return names


def _collect_assign_targets(target: ast.expr, out: Set[str]) -> None:
    if isinstance(target, ast.Name):
        out.add(target.id)
    elif isinstance(target, (ast.Tuple, ast.List)):
        for elt in target.elts:
            _collect_assign_targets(elt, out)


# ── Module-path mapping ─────────────────────────────────────────────────────

def _module_to_candidate_paths(module: str) -> List[str]:
    """Map a dotted first-party module to candidate repo paths.
    `app.x.y` -> backend/app/x/y.py and backend/app/x/y/__init__.py.
    `backend.x` -> backend/x.py and backend/x/__init__.py."""
    if module.startswith("app."):
        base = "backend/" + module.replace(".", "/")
    elif module.startswith("backend."):
        base = module.replace(".", "/")
    else:
        return []
    return [base + ".py", base + "/__init__.py"]


def _path_to_module(path: str) -> Optional[str]:
    """Inverse: backend/app/main.py -> app.main; backend/foo.py -> foo.
    Returns None for non-.py paths."""
    if not path.endswith(".py"):
        return None
    if path.startswith("backend/app/"):
        return path[len("backend/"):].replace("/", ".").rsplit(".", 1)[0]
    if path.startswith("backend/"):
        return path[len("backend/"):].replace("/", ".").rsplit(".", 1)[0]
    return None


# ── Public detectors ────────────────────────────────────────────────────────

def syntax_error_of(content: str, path: str) -> Optional[str]:
    """Return a description if `content` (a .py file) doesn't parse, else None."""
    if not path.endswith(".py"):
        return None
    try:
        ast.parse(content)
        return None
    except SyntaxError as e:
        return f"{path}: Python syntax error at line {e.lineno}: {e.msg}"


def detect_bad_imports(
    new_content: str,
    original_content: str,
    file_tree: List[str],
) -> List[str]:
    """First-party imports in new_content that are NOT in the original AND
    don't map to a real path in file_tree. These are the classic
    hallucinated `from app.db.database import init_db` failures.
    """
    new_coll, new_err = _collect_imports(new_content)
    if new_err is not None:
        # Syntax error is reported separately by syntax_error_of — don't
        # double-report here.
        return []
    orig_coll, _ = _collect_imports(original_content) if original_content else (_ImportCollector(), None)

    tree_set = set(file_tree)
    bad: List[str] = []

    # First-party `from X import …` and `import X` modules new in this patch.
    orig_modules = set(orig_coll.module_imports) | orig_coll.plain_modules
    new_modules = set(new_coll.module_imports) | new_coll.plain_modules

    for module in sorted(new_modules - orig_modules):
        if module.startswith("."):
            continue  # relative imports resolved by stale-named check, not here
        if not any(module.startswith(p) for p in _FIRST_PARTY_PREFIXES):
            continue  # stdlib / third-party — out of scope
        candidates = _module_to_candidate_paths(module)
        if candidates and not any(c in tree_set for c in candidates):
            bad.append(module)
    return bad


def detect_stale_named_imports(
    new_content: str,
    target_files: Dict[str, str],
    coder_files: List[Any],   # List[CoderFile]; duck-typed (.path, .new_content)
) -> List[str]:
    """`from X import a, b` where X is a module visible in this patch's final
    state (target_files merged with coder_files) but a/b aren't defined in
    X's final content.

    This is the test-imports-stale-symbol failure mode: a test file imports
    `_contains_dangerous_pattern` from `app.main` but the rewritten main.py
    never defines it.
    """
    new_coll, new_err = _collect_imports(new_content)
    if new_err is not None:
        return []

    # Build module -> final source content for everything in this patch.
    final_content: Dict[str, str] = dict(target_files)
    for cf in coder_files:
        final_content[cf.path] = cf.new_content

    by_module: Dict[str, str] = {}
    for path, src in final_content.items():
        mod = _path_to_module(path)
        if mod is not None:
            by_module[mod] = src

    suspect: List[str] = []
    for module, names in new_coll.module_imports.items():
        if module.startswith("."):
            continue  # relative — would need package context; skip for now
        target_src = by_module.get(module)
        if target_src is None:
            continue  # not a module we can see — out of scope
        defined = module_exports(target_src)
        for name in sorted(names):
            if name not in defined:
                suspect.append(f"{name} from {module}")
    return suspect
