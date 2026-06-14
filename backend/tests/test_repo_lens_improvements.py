"""RepoLens improvements — entry points, architecture, scoring, risks, tech stack.

Tests are grouped by concern.  Each group covers the heuristic behavior that the
production RepoLens agent must produce without any LLM calls.
"""
import pytest
from app.agents.repo_lens_agent import (
    RepoLensAgent,
    _has_top_level_dir,
    _find_files_by_name,
    _is_false_entry_point,
    _detect_architecture_pattern,
)
from app.schemas.agent_schemas import ArchitectureRisk


# ── Pure helper functions ─────────────────────────────────────────────────────

class TestHasTopLevelDir:
    def test_present_as_path_prefix(self):
        assert _has_top_level_dir(["frontend/src/App.tsx"], "frontend")

    def test_missing(self):
        assert not _has_top_level_dir(["src/App.tsx"], "frontend")

    def test_exact_name_match(self):
        assert _has_top_level_dir(["frontend"], "frontend")

    def test_partial_name_not_matched(self):
        assert not _has_top_level_dir(["frontend-extra/x.ts"], "frontend")


class TestFindFilesByName:
    def test_finds_by_exact_name(self):
        tree = ["a/b/main.py", "c/d/server.ts", "e/README.md"]
        result = _find_files_by_name(tree, {"main.py", "server.ts"})
        assert sorted(result) == ["a/b/main.py", "c/d/server.ts"]

    def test_empty_when_none_match(self):
        assert _find_files_by_name(["src/utils.ts"], {"main.py"}) == []


class TestIsFalseEntryPoint:
    def test_components_dir(self):
        assert _is_false_entry_point("frontend/src/components/index.ts")

    def test_hooks_dir(self):
        assert _is_false_entry_point("frontend/src/hooks/index.ts")

    def test_utils_dir(self):
        assert _is_false_entry_point("frontend/src/utils/main.ts")

    def test_types_dir(self):
        assert _is_false_entry_point("frontend/src/types/index.ts")

    def test_lib_dir(self):
        assert _is_false_entry_point("frontend/src/lib/index.ts")

    def test_store_dir(self):
        assert _is_false_entry_point("frontend/src/store/index.ts")

    def test_constants_dir(self):
        assert _is_false_entry_point("frontend/src/constants/index.ts")

    def test_dist_dir(self):
        assert _is_false_entry_point("dist/main.js")

    def test_real_src_main_tsx_not_false(self):
        assert not _is_false_entry_point("frontend/src/main.tsx")

    def test_real_backend_main_not_false(self):
        assert not _is_false_entry_point("backend/app/main.py")


# ── Architecture detection ─────────────────────────────────────────────────────

class TestArchitectureDetection:
    def _run(self, tree):
        return _detect_architecture_pattern(tree)

    def test_frontend_and_backend_dirs_give_fullstack(self):
        tree = [
            "frontend/src/App.tsx",
            "frontend/src/main.tsx",
            "backend/app/main.py",
            "backend/requirements.txt",
        ]
        assert self._run(tree) == "fullstack"

    def test_only_frontend_gives_frontend(self):
        tree = ["frontend/src/App.tsx", "frontend/src/main.tsx", "package.json"]
        assert self._run(tree) == "frontend"

    def test_only_python_gives_backend(self):
        tree = ["app/main.py", "app/routes.py", "requirements.txt"]
        assert self._run(tree) == "backend"

    def test_packages_and_apps_gives_monorepo(self):
        tree = [
            "packages/core/index.ts",
            "packages/ui/index.ts",
            "apps/web/index.ts",
        ]
        assert self._run(tree) == "monorepo"

    def test_small_ts_only_gives_frontend(self):
        # TypeScript-only repo with no backend code → "frontend" (not "library")
        tree = ["src/index.ts", "src/utils.ts", "package.json"]
        assert self._run(tree) == "frontend"

    def test_unknown_when_very_few_files(self):
        assert self._run(["README.md"]) == "unknown"

    def test_only_frontend_is_not_fullstack(self):
        tree = ["frontend/src/App.tsx", "frontend/src/main.tsx"]
        assert self._run(tree) != "fullstack"

    def test_only_backend_is_not_fullstack(self):
        tree = ["backend/app/main.py", "backend/requirements.txt"]
        assert self._run(tree) != "fullstack"


# ── Entry points ──────────────────────────────────────────────────────────────

class TestEntryPoints:
    def _run(self, tree):
        return RepoLensAgent()._entry_points(tree)

    def test_index_ts_in_components_excluded(self):
        tree = ["frontend/src/components/index.ts", "backend/app/main.py"]
        assert "frontend/src/components/index.ts" not in self._run(tree)

    def test_index_ts_in_hooks_excluded(self):
        tree = ["frontend/src/hooks/index.ts", "backend/app/main.py"]
        assert "frontend/src/hooks/index.ts" not in self._run(tree)

    def test_real_main_py_included(self):
        assert "backend/app/main.py" in self._run(["backend/app/main.py"])

    def test_real_index_ts_at_src_included(self):
        assert "frontend/src/index.ts" in self._run(["frontend/src/index.ts"])

    def test_main_tsx_included(self):
        assert "frontend/src/main.tsx" in self._run(["frontend/src/main.tsx"])

    def test_app_tsx_included(self):
        assert "frontend/src/App.tsx" in self._run(["frontend/src/App.tsx"])

    def test_index_ts_in_types_excluded(self):
        assert "src/types/index.ts" not in self._run(["src/types/index.ts"])

    def test_index_ts_in_lib_excluded(self):
        assert "src/lib/index.ts" not in self._run(["src/lib/index.ts"])

    def test_index_ts_in_store_excluded(self):
        assert "src/store/index.ts" not in self._run(["src/store/index.ts"])

    def test_max_8_results(self):
        # Many entry points — must cap at 8
        tree = [f"dir{i}/main.py" for i in range(20)]
        assert len(self._run(tree)) <= 8

    def test_higher_priority_file_comes_first(self):
        # main.tsx (priority 90) should beat App.tsx (70) and index.tsx (55)
        tree = ["src/index.tsx", "src/App.tsx", "src/main.tsx"]
        result = self._run(tree)
        assert result[0] == "src/main.tsx"


# ── Scoring ────────────────────────────────────────────────────────────────────

class TestSeverityWeightedScore:
    """Risk-only scoring — no hardcoded per-flag deductions."""

    def _score(self, risks):
        return RepoLensAgent()._score(risks)

    def test_no_risks_gives_100(self):
        assert self._score([]) == 100

    def test_single_critical_deducts_25(self):
        r = [ArchitectureRisk(risk="x", impact="critical", category="security")]
        assert self._score(r) == 75

    def test_single_high_deducts_18(self):
        r = [ArchitectureRisk(risk="x", impact="high", category="ci_cd")]
        assert self._score(r) == 82

    def test_single_medium_deducts_8(self):
        r = [ArchitectureRisk(risk="x", impact="medium", category="config")]
        assert self._score(r) == 92

    def test_single_low_deducts_4(self):
        r = [ArchitectureRisk(risk="x", impact="low", category="docs")]
        assert self._score(r) == 96

    def test_critical_deducts_more_than_five_lows(self):
        one_critical = [ArchitectureRisk(risk="c", impact="critical", category="security")]
        five_lows = [ArchitectureRisk(risk=f"l{i}", impact="low", category="docs") for i in range(5)]
        assert self._score(one_critical) < self._score(five_lows)

    def test_critical_deducts_more_than_high(self):
        assert self._score([ArchitectureRisk(risk="c", impact="critical", category="security")]) < \
               self._score([ArchitectureRisk(risk="h", impact="high", category="ci_cd")])

    def test_score_never_below_zero(self):
        # Many criticals shouldn't push score below 0
        risks = [ArchitectureRisk(risk=f"c{i}", impact="critical", category="security") for i in range(20)]
        assert self._score(risks) == 0

    def test_score_never_above_100(self):
        # Empty risk list stays at 100
        assert self._score([]) == 100

    def test_medium_confidence_risk_halved(self):
        low_conf = ArchitectureRisk(
            risk="lint", impact="medium", category="config", confidence="medium"
        )
        high_conf = ArchitectureRisk(
            risk="lint", impact="medium", category="config", confidence="high"
        )
        # medium confidence → weight 8//2 = 4 deduction; high confidence → 8 deduction
        assert self._score([low_conf]) == 96
        assert self._score([high_conf]) == 92

    def test_low_confidence_risk_ignored(self):
        r = ArchitectureRisk(risk="x", impact="critical", category="security", confidence="low")
        assert self._score([r]) == 100


# ── Risk detection ─────────────────────────────────────────────────────────────

class TestRiskDetection:
    def _risks(self, tree, kf=None, has_ci=True, has_docker=True, has_tests=True):
        return RepoLensAgent()._assess_risks(tree, kf or {}, has_ci, has_docker, has_tests)

    def _risk_titles(self, *args, **kwargs):
        return [r.risk for r in self._risks(*args, **kwargs)]

    def test_no_ci_generates_high_risk(self):
        risks = self._risks([], has_ci=False)
        ci_risks = [r for r in risks if r.impact == "high" and "CI" in r.risk]
        assert len(ci_risks) == 1

    def test_no_tests_generates_high_risk(self):
        risks = self._risks([], has_tests=False)
        test_risks = [r for r in risks if r.impact == "high" and "test" in r.risk.lower()]
        assert len(test_risks) == 1

    def test_env_file_committed_is_critical(self):
        risks = self._risks([".env"])
        critical = [r for r in risks if r.impact == "critical"]
        assert len(critical) == 1
        assert all(r.evidence == ".env" for r in critical)

    def test_no_lockfile_with_package_json_is_medium(self):
        kf = {"package.json": '{"dependencies": {"react": "^18"}}'}
        tree = ["package.json"]
        risks = self._risks(tree, kf=kf)
        lockfile_risks = [r for r in risks if "lockfile" in r.risk.lower()]
        assert len(lockfile_risks) == 1
        assert lockfile_risks[0].impact == "medium"

    def test_lockfile_present_no_lockfile_risk(self):
        kf = {"package.json": '{"dependencies": {}}'}
        tree = ["package.json", "package-lock.json"]
        risks = self._risks(tree, kf=kf)
        assert not any("lockfile" in r.risk.lower() for r in risks)

    def test_no_env_example_with_backend_is_low(self):
        risks = self._risks(["app/main.py"])
        env_risks = [r for r in risks if ".env.example" in r.risk.lower()]
        assert len(env_risks) == 1
        assert env_risks[0].impact == "low"

    def test_env_example_present_no_risk(self):
        risks = self._risks(["app/main.py", ".env.example"])
        assert not any(".env.example" in r.risk.lower() for r in risks)

    def test_ts_files_without_tsconfig_is_medium(self):
        risks = self._risks(["src/main.ts", "src/app.ts"])
        ts_risks = [r for r in risks if "tsconfig" in r.risk.lower()]
        assert len(ts_risks) == 1
        assert ts_risks[0].impact == "medium"

    def test_tsconfig_present_no_ts_risk(self):
        risks = self._risks(["src/main.ts", "tsconfig.json"])
        assert not any("tsconfig" in r.risk.lower() for r in risks)

    def test_no_linting_config_is_low_medium(self):
        risks = self._risks([])
        lint_risks = [r for r in risks if "lint" in r.risk.lower() or "format" in r.risk.lower()]
        assert len(lint_risks) == 1
        assert lint_risks[0].impact == "low"

    def test_eslint_present_no_lint_risk(self):
        risks = self._risks([".eslintrc.js"])
        assert not any("lint" in r.risk.lower() for r in risks)

    def test_all_risks_have_evidence_field(self):
        tree = ["app/main.py"]
        risks = self._risks(tree, has_ci=False, has_tests=False)
        for r in risks:
            assert r.evidence is not None, f"Risk '{r.risk}' missing evidence"

    def test_all_risks_have_confidence_field(self):
        tree = ["app/main.py"]
        risks = self._risks(tree, has_ci=False, has_tests=False)
        for r in risks:
            assert r.confidence in ("high", "medium", "low"), f"Bad confidence on '{r.risk}'"

    def test_no_dockerfile_only_flagged_for_backend_repos(self):
        # Pure frontend — no Python/Go — should NOT get dockerfile risk
        risks = self._risks(["src/App.tsx", "src/main.tsx"], has_docker=False)
        docker_risks = [r for r in risks if "dockerfile" in r.risk.lower()]
        assert len(docker_risks) == 0

    def test_no_dockerfile_flagged_for_backend_repo(self):
        risks = self._risks(["app/main.py"], has_docker=False)
        docker_risks = [r for r in risks if "dockerfile" in r.risk.lower()]
        assert len(docker_risks) == 1


# ── Tech stack detection ──────────────────────────────────────────────────────

class TestTechStackDetection:
    def _stack(self, tree, kf=None):
        return set(RepoLensAgent()._detect_stack(tree, kf or {}))

    def test_react_from_package_json(self):
        kf = {"package.json": '{"dependencies": {"react": "^18"}}'}
        assert "React" in self._stack([], kf)

    def test_vite_from_vite_config_ts(self):
        assert "Vite" in self._stack(["vite.config.ts"])

    def test_tailwind_from_tailwind_config(self):
        assert "Tailwind CSS" in self._stack(["tailwind.config.ts"])

    def test_github_actions_from_workflow_dir(self):
        assert "GitHub Actions" in self._stack([".github/workflows/ci.yml"])

    def test_typescript_from_tsconfig(self):
        assert "TypeScript" in self._stack(["tsconfig.json"])

    def test_python_from_requirements(self):
        kf = {"requirements.txt": "fastapi==0.100\npydantic"}
        assert "Python" in self._stack([], kf)
        assert "FastAPI" in self._stack([], kf)

    def test_go_from_go_mod(self):
        kf = {"go.mod": "module github.com/example/app\n\ngo 1.21"}
        assert "Go" in self._stack([], kf)

    def test_docker_from_dockerfile(self):
        assert "Docker" in self._stack(["Dockerfile"])

    def test_next_from_next_config(self):
        assert "Next.js" in self._stack(["next.config.ts"])
