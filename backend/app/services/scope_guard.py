"""
ScopeGuard — catch scope-drift in whole-file rewrites that the pytest gate
cannot see.

Motivation (two real misses the pass-count gate let through):
  1. A "JSON body sanitization" patch was a 197-line rewrite of main.py that
     silently DELETED the `_lifespan` startup hook and `_BodyReplayRequest`.
     pytest stayed green because nothing *tests* the lifespan hook.
  2. A "add .gitignore entry for bytecode" patch actually DELETED the
     `reports/`, `junit.xml`, `dist/`, `build/` entries while claiming to add
     one. pytest stayed green because nothing tests .gitignore.

The common shape: a full-file replacement that removes pre-existing top-level
definitions (functions / classes / assignments) or shrinks a protected
config file, where the removal is incidental to the stated task — pure
collateral damage, invisible to a test-count gate.

What this guard does (conservative, biased toward false-NEGATIVE so it never
blocks a legitimate refactor):

  • For .py files: parse BOTH the original and the new content with `ast`.
    Flag any MODULE-LEVEL name (def / async def / class / assigned name) that
    existed in the original and is GONE in the new content. Renames and real
    deletions are rare in single-finding patches; when they're intended the
    Coder can say so (see allow-list below). A dropped public symbol that
    nothing imports is exactly the lifespan-hook miss.

  • For protected non-code files (.gitignore, requirements*.txt, ci.yml, etc.):
    flag LINE deletions — any non-blank, non-comment line present in the
    original and absent from the new content. These files are append-mostly;
    a patch that removes lines from them is almost always drift.

  • Syntactically-broken new .py content is NOT this guard's job (ast_lint /
    the smoke import already catch that) — if the new content doesn't parse we
    return no findings and let those gates speak.

The guard returns a list of human-readable issue strings (empty == clean),
mirroring `_lint_coder_output` so the orchestrator and loop treat it the
same way.
"""
from __future__ import annotations

import ast
import logging
import re
from typing import Dict, List, Optional, Set

logger = logging.getLogger("shipmate.scope_guard")

# Non-.py files whose lines are append-mostly — removing existing lines is
# treated as drift. Matched by basename (case-insensitive) or suffix.
_PROTECTED_BASENAMES = {
    ".gitignore",
    ".dockerignore",
    ".gitattributes",
}
_PROTECTED_SUFFIXES = (
    "requirements.txt",
    "requirements-dev.txt",
)
# Path fragments that mark a protected file regardless of basename.
_PROTECTED_FRAGMENTS = (
    ".github/workflows/",   # CI yaml — dropping a job/step is drift
)

# When the Coder explicitly states intent to remove/refactor, we relax the
# .py symbol-deletion check (a genuine cleanup, e.g. the duplicate-sanitizer
# refactor, legitimately deletes defs). Matched against the file rationale +
# the overall summary.
# Stem prefixes — no trailing \b (these are deliberately partial words:
# "remov" matches remove/removing/removal, "consolidat" matches
# consolidate/consolidating, etc.).
_REMOVAL_INTENT_RE = re.compile(
    r"\b(remov|delet|drop|deprecat|consolidat|dedup|de-dup|rewrit|"
    r"merg\w*\s+duplicat|replac\w*\s+(the\s+)?(duplicate|shadow)|"
    r"unus|dead\s+code|clean\s*up|refactor)",
    re.IGNORECASE,
)

# Module-level dunder/throwaway names we don't care about losing.
_IGNORABLE_NAMES = {"_", "__all__"}

# Mass-deletion heuristic for ANY other text file (README, docs, configs not
# in the protected list). A patch that removes a large FRACTION *and* a
# meaningful absolute COUNT of a file's existing lines is almost always drift
# — e.g. a "add a CI badge" patch that also deleted 626 lines of README.
# Both thresholds must trip, so small edits and short files never false-fire.
_MASS_DELETE_FRACTION = 0.40
_MASS_DELETE_MIN_LINES = 30


def _is_protected_nonpy(path: str) -> bool:
    low = path.lower()
    base = low.rsplit("/", 1)[-1]
    if base in _PROTECTED_BASENAMES:
        return True
    if any(low.endswith(s) for s in _PROTECTED_SUFFIXES):
        return True
    if any(frag in low for frag in _PROTECTED_FRAGMENTS):
        return True
    return False


def _module_level_names(source: str) -> Optional[Set[str]]:
    """Top-level def/class/assignment names in `source`. None if it doesn't
    parse (caller then skips — parse errors are another gate's problem)."""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return None
    names: Set[str] = set()
    for node in tree.body:  # body == module level only
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for tgt in node.targets:
                if isinstance(tgt, ast.Name):
                    names.add(tgt.id)
        elif isinstance(node, ast.AnnAssign):
            if isinstance(node.target, ast.Name):
                names.add(node.target.id)
    return names - _IGNORABLE_NAMES


def _significant_lines(text: str) -> Set[str]:
    """Non-blank, non-comment lines (stripped) for protected-file diffing."""
    out: Set[str] = set()
    for raw in text.splitlines():
        s = raw.strip()
        if not s or s.startswith("#"):
            continue
        out.add(s)
    return out


def check_file(
    path: str,
    original: str,
    new_content: str,
    rationale: str = "",
    summary: str = "",
) -> List[str]:
    """Scope-drift issues for a single file (empty == clean).

    `original` is the current repo content ("" for a brand-new file — new
    files can't drop anything, so they're always clean here). `rationale` is
    the Coder's per-file reason; `summary` the overall patch summary. Both
    feed the removal-intent relaxation.
    """
    # New file (or one we couldn't fetch) — nothing pre-existing to drop.
    if not original.strip():
        return []

    intent_text = f"{rationale}\n{summary}"
    removal_ok = bool(_REMOVAL_INTENT_RE.search(intent_text))

    if path.endswith(".py"):
        if removal_ok:
            return []  # Coder declared a cleanup/refactor — trust + let pytest gate verify
        before = _module_level_names(original)
        after = _module_level_names(new_content)
        if before is None or after is None:
            return []  # un-parseable on either side — defer to ast_lint / smoke
        dropped = sorted(before - after)
        if dropped:
            shown = ", ".join(dropped[:8])
            more = f" (+{len(dropped) - 8} more)" if len(dropped) > 8 else ""
            return [
                f"{path}: whole-file rewrite DROPS {len(dropped)} pre-existing "
                f"top-level definition(s): {shown}{more}. If this removal is "
                f"intentional, say so in the rationale (e.g. 'remove unused X'); "
                f"otherwise it is scope drift — restore the dropped symbol(s)."
            ]
        return []

    if _is_protected_nonpy(path):
        if removal_ok:
            return []
        before_lines = _significant_lines(original)
        after_lines = _significant_lines(new_content)
        dropped = sorted(before_lines - after_lines)
        if dropped:
            shown = "; ".join(d[:60] for d in dropped[:6])
            more = f" (+{len(dropped) - 6} more)" if len(dropped) > 6 else ""
            return [
                f"{path}: rewrite REMOVES {len(dropped)} existing line(s) from a "
                f"protected file: {shown}{more}. These files are append-mostly; "
                f"removing lines is almost always scope drift. Keep the existing "
                f"lines unless the rationale explains the removal."
            ]
        return []

    # Catch-all for any other text file (README, docs, etc.): a mass deletion
    # that nukes a large fraction of the file is drift the .py / protected
    # checks above won't see. (PR #26 added a CI badge to README and deleted
    # 626 lines of architecture docs — no gate caught it.)
    if removal_ok:
        return []
    before_lines = _significant_lines(original)
    after_lines = _significant_lines(new_content)
    if not before_lines:
        return []
    removed = before_lines - after_lines
    frac = len(removed) / len(before_lines)
    if len(removed) >= _MASS_DELETE_MIN_LINES and frac >= _MASS_DELETE_FRACTION:
        return [
            f"{path}: rewrite DELETES {len(removed)} of {len(before_lines)} "
            f"existing lines ({frac:.0%} of the file). A change that removes "
            f"this much content is almost always scope drift (e.g. a small "
            f"edit that also nuked the rest of the file). If the deletion is "
            f"intentional, say so in the rationale; otherwise preserve the "
            f"existing content."
        ]
    return []


def check_patch(
    files: List[Dict[str, str]],
    originals: Dict[str, str],
    summary: str = "",
) -> List[str]:
    """Run the scope guard across a whole patch.

    `files`     : [{"path","new_content","rationale"?}, ...] (Coder output,
                  serialized — same shape the pytest gate consumes).
    `originals` : path -> current repo content (the orchestrator's
                  `target_files`; missing key == new file).
    `summary`   : the Coder's overall patch summary.

    Returns the concatenated issue list (empty == clean).
    """
    issues: List[str] = []
    for f in files:
        path = f["path"]
        new_content = f.get("new_content", "")
        rationale = f.get("rationale", "")
        original = originals.get(path, "")
        issues.extend(check_file(path, original, new_content, rationale, summary))
    return issues
