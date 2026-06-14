"""Repo map for the Coder brief (Phase 1 — prevention half of the import-
hallucination defense).

A dogfood loop watched the Coder invent `from app.services.analysis_service
import ...` — a module that does not exist — and ast_lint rejected it only
AFTER a wasted Bedrock round-trip. repo_map shows the model the REAL module
layout + symbols up front so it never guesses.

These tests pin the contract the orchestrator relies on:
  • the skeleton lists the real neighbors of the target and excludes vendor/
    build/cache noise;
  • the symbol index advertises ONLY real top-level exports, via the SAME
    ast_lint.module_exports the stale-import lint uses (so map and lint can
    never disagree);
  • the GOLDEN anti-hallucination assertion: a fabricated sibling
    (`app.services.analysis_service`) is absent from the map because it isn't
    in the tree — the model has nothing to copy;
  • bounds: a huge tree stays under the char cap and is marked truncated;
  • fail-open: junk input returns "" rather than raising.
"""
from app.services import ast_lint, repo_map

# A realistic ShipMate-shaped tree. `analysis_service.py` deliberately does NOT
# exist — it's the module the live loop hallucinated.
_FILE_TREE = [
    "backend/app/main.py",
    "backend/app/services/llm_service.py",
    "backend/app/services/scoring_service.py",
    "backend/app/services/report_service.py",
    "backend/app/services/repo_map.py",
    "backend/app/agents/coder_agent.py",
    "backend/app/api/routes/analysis.py",
    "backend/tests/test_llm_service.py",
    # Noise that MUST be excluded from the skeleton:
    "backend/app/services/__pycache__/llm_service.cpython-311.pyc",
    "node_modules/react/index.js",
    "frontend/dist/assets/index-abc123.js",
    ".git/HEAD",
    "frontend/src/App.tsx",
]

_SCORING_SRC = '''
import os
from typing import Dict

WEIGHTS = {"a": 1}

class ScoringService:
    @staticmethod
    def calculate(x):
        return x

def recommendation(score: int) -> str:
    return "ship"

def _private_helper():  # still a real export, just private
    return 1
'''

_KEY_FILES = {
    "backend/app/services/scoring_service.py": _SCORING_SRC,
    "backend/app/services/llm_service.py": "def enhance():\n    return 1\n",
}

# Target the Coder is about to edit — drives skeleton scoping.
_TARGETS = ["backend/app/services/scoring_service.py"]


class TestSkeleton:
    def test_lists_real_siblings_of_target(self):
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        # Siblings in the same package show up by basename.
        assert "llm_service.py" in m
        assert "report_service.py" in m
        assert "scoring_service.py" in m

    def test_excludes_vendor_build_and_cache_noise(self):
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        assert "__pycache__" not in m
        assert ".pyc" not in m
        assert "node_modules" not in m
        assert "/dist/" not in m and "dist/assets" not in m
        assert ".git" not in m

    def test_scopes_to_package_not_whole_repo(self):
        # Editing a backend service should not drag in the frontend tree.
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        assert "App.tsx" not in m


class TestSymbolIndex:
    def test_advertises_real_exports_only(self):
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        # The module is named by dotted path, and its real symbols are listed.
        assert "app.services.scoring_service" in m
        assert "ScoringService" in m
        assert "recommendation" in m
        assert "WEIGHTS" in m

    def test_symbols_match_ast_lint_source_of_truth(self):
        # repo_map MUST advertise exactly what ast_lint.module_exports reports —
        # that shared function is the contract that keeps map (prevention) and
        # stale-import lint (cure) from ever disagreeing.
        exports = ast_lint.module_exports(_SCORING_SRC)
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        for sym in exports:
            assert sym in m, f"export {sym} missing from repo map"

    def test_public_symbols_listed_before_private(self):
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        # _private_helper is a real export but should trail the public ones.
        assert m.index("ScoringService") < m.index("_private_helper")

    def test_init_advertised_as_idiomatic_package_name(self):
        # Regression for the review finding: a package __init__.py must be
        # advertised as the IDIOMATIC 'app.services', not 'app.services.__init__'
        # (a valid but ugly import the model would copy).
        tree = _FILE_TREE + ["backend/app/services/__init__.py"]
        key = {
            "backend/app/services/__init__.py": "from .scoring_service import ScoringService\nVERSION = '1'\n",
        }
        m = repo_map.build_repo_map(tree, key, ["backend/app/services/__init__.py"])
        assert "app.services.__init__" not in m
        assert "app.services:" in m  # idiomatic package name, with its symbols


class TestRootLevelScoping:
    """Regression for the adversarial-review finding: a root-level target
    (requirements.txt / .gitignore / README.md — all reachable from
    _resolve_target_paths) produced a '' focus dir that _in_scope treated as
    'everything', dragging the whole repo (incl. the frontend tree) into a
    backend map and defeating package scoping."""

    def test_pure_root_target_does_not_pull_whole_repo(self):
        m = repo_map.build_repo_map(_FILE_TREE, {}, ["requirements.txt"])
        # A root-level target scopes to root-level files only — not the
        # entire backend + frontend tree.
        assert "App.tsx" not in m
        assert "llm_service.py" not in m

    def test_mixed_root_and_package_target_keeps_scope(self):
        # The verifier's exact reproduction: a secrets finding resolving to
        # ['backend/app/main.py', '.gitignore'] must NOT leak the frontend.
        m = repo_map.build_repo_map(
            _FILE_TREE, {}, ["backend/app/main.py", ".gitignore"],
        )
        assert "App.tsx" not in m
        # The backend package neighbors are still in scope.
        assert "main.py" in m
        assert "llm_service.py" in m


class TestGoldenAntiHallucination:
    """The Rosetta-stone case: the symbol the live loop fabricated must be
    ABSENT from the map for this repo, because it genuinely isn't in the tree.
    With the real namespace in front of it, the model has nothing to copy."""

    def test_hallucinated_module_absent(self):
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        assert "analysis_service" not in m

    def test_real_analysis_route_present_but_not_a_service(self):
        # `app.api.routes.analysis` is real; `app.services.analysis_service`
        # is the hallucination. The map shows the former, never the latter.
        m = repo_map.build_repo_map(_FILE_TREE, _KEY_FILES, _TARGETS)
        assert "analysis.py" in m
        assert "app.services.analysis_service" not in m


class TestBounds:
    def test_huge_tree_stays_under_char_cap(self):
        big_tree = [f"backend/app/services/mod_{i}.py" for i in range(5000)]
        big_tree.append("backend/app/services/scoring_service.py")
        m = repo_map.build_repo_map(
            big_tree, _KEY_FILES, _TARGETS, max_chars=4000,
        )
        assert len(m) <= 4000 + 60  # cap + the truncation marker line
        assert "truncated" in m.lower()


class TestFailOpen:
    def test_empty_inputs_return_empty_string(self):
        assert repo_map.build_repo_map([], {}, []) == ""

    def test_no_parseable_content_still_renders_skeleton(self):
        # No key_files content → no symbol index, but the skeleton alone is
        # still useful (it's what kills the missing-module hallucination).
        m = repo_map.build_repo_map(_FILE_TREE, {}, _TARGETS)
        assert "scoring_service.py" in m
        assert "Exported symbols" not in m

    def test_malformed_tree_does_not_raise(self):
        # None entries / non-string junk must not blow up an actuate.
        m = repo_map.build_repo_map(
            ["backend/app/services/scoring_service.py", "", None],  # type: ignore[list-item]
            _KEY_FILES, _TARGETS,
        )
        assert isinstance(m, str)
        assert "scoring_service.py" in m
