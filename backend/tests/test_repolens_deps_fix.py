"""RepoLens dependency / architecture fixes (the empty-deps monorepo bug).

The root bug: _select_key_files matched BARE names ('requirements.txt') against
a FULL-PATH tree ('backend/requirements.txt'), so in any monorepo / frontend+
backend split the manifests were never fetched → _extract_deps was a no-op →
dependency_summary={} → every downstream agent (PlanForge, GuardRail, TestPilot)
inherited a tech-stack-blind context.

Three fixes here:
  • _select_key_files matches BASENAMES across the tree and collects multiple
    copies of recurring manifests (one per package in a monorepo);
  • _extract_deps aggregates across ALL manifest copies (the basename key is
    last-write-wins, so it iterates full-path keys), merged + de-duped;
  • _architecture adds a 'tiered_app' pattern for a frontend+backend split
    (ShipMate's own shape), which used to misclassify as 'monolith'.
"""
from app.services.repo_analysis_service import (
    RepoAnalysisService, _MAX_MANIFEST_COPIES, _MULTI_COPY_MANIFESTS,
)
from app.agents.repo_lens_agent import RepoLensAgent


# ── Fix 1 — _select_key_files matches subdirectory manifests ──────────────────
class TestSelectKeyFiles:
    def test_subdirectory_manifests_are_selected(self):
        tree = [
            "README.md",
            "backend/requirements.txt",
            "frontend/package.json",
            "backend/app/main.py",
            "Dockerfile",
        ]
        sel = RepoAnalysisService._select_key_files(tree)
        # The whole point: full subdirectory paths, not bare names that 404.
        assert "backend/requirements.txt" in sel
        assert "frontend/package.json" in sel
        # And they are REAL tree paths (would actually fetch).
        assert all(s in tree for s in sel)

    def test_multiple_copies_of_recurring_manifest_collected(self):
        tree = [
            "backend/requirements.txt",
            "services/auth/requirements.txt",
            "services/billing/requirements.txt",
            "app/main.py",
        ]
        sel = RepoAnalysisService._select_key_files(tree)
        reqs = [s for s in sel if s.endswith("requirements.txt")]
        assert len(reqs) >= 2  # not just the first

    def test_shallowest_copy_leads(self):
        tree = ["a/b/c/requirements.txt", "requirements.txt", "x/requirements.txt"]
        sel = RepoAnalysisService._select_key_files(tree)
        reqs = [s for s in sel if s.endswith("requirements.txt")]
        assert reqs[0] == "requirements.txt"  # root copy first

    def test_flat_repo_still_works(self):
        # The original (root-level manifests) case must not regress.
        tree = ["requirements.txt", "package.json", "main.py", "README.md"]
        sel = RepoAnalysisService._select_key_files(tree)
        assert "requirements.txt" in sel and "package.json" in sel

    def test_manifest_set_and_cap_sane(self):
        assert "package.json" in _MULTI_COPY_MANIFESTS
        assert "requirements.txt" in _MULTI_COPY_MANIFESTS
        assert 2 <= _MAX_MANIFEST_COPIES <= 10


# ── Fix 2 — _extract_deps aggregates across all manifest copies ───────────────
class TestExtractDeps:
    def _agent(self):
        return RepoLensAgent()

    def test_aggregates_across_two_requirements_files(self):
        # key_files shape mirrors _fetch_files: basename alias (last write) +
        # full-path keys. The bare alias alone would hide the backend deps.
        be = "fastapi==0.110\nboto3>=1.34\n"
        svc = "pydantic==2.6\n"
        kf = {
            "requirements.txt": svc,                  # last-write alias = services
            "backend/requirements.txt": be,
            "services/x/requirements.txt": svc,
        }
        deps = self._agent()._extract_deps(kf)
        assert "boto3" in deps["python"]              # backend dep recovered
        assert "fastapi" in deps["python"]
        assert "pydantic" in deps["python"]

    def test_dedupes_across_copies(self):
        dup = "fastapi==0.110\n"
        kf = {
            "requirements.txt": dup,
            "backend/requirements.txt": dup,
            "frontend/requirements.txt": dup,
        }
        deps = self._agent()._extract_deps(kf)
        assert deps["python"].count("fastapi") == 1

    def test_npm_aggregated_and_parsed(self):
        kf = {
            "frontend/package.json": '{"dependencies":{"react":"18"},"devDependencies":{"vite":"5"}}',
            "package.json": '{"dependencies":{"react":"18"},"devDependencies":{"vite":"5"}}',
        }
        deps = self._agent()._extract_deps(kf)
        assert set(deps["npm"]) == {"react", "vite"}

    def test_flat_repo_root_manifest_only(self):
        # No '/'-keyed entry (truly flat repo) → fall back to the bare key.
        kf = {"requirements.txt": "flask==3\n"}
        deps = self._agent()._extract_deps(kf)
        assert deps["python"] == ["flask"]

    def test_no_manifests_empty_deps(self):
        assert self._agent()._extract_deps({"main.py": "x=1\n"}) == {}


# ── Fix 3 — tiered_app architecture pattern ───────────────────────────────────
class TestArchitecture:
    def _arch(self, tree):
        return RepoLensAgent()._architecture(tree)

    def test_frontend_backend_split_is_tiered_app(self):
        assert self._arch(["backend/app/main.py", "frontend/src/App.tsx"]) == "tiered_app"

    def test_client_server_split_is_tiered_app(self):
        assert self._arch(["server/main.go", "client/index.ts"]) == "tiered_app"

    def test_monorepo_still_wins_over_tiered(self):
        # packages/ is a stronger monorepo signal — keep it.
        assert self._arch(["packages/a/x.ts", "frontend/y.tsx", "backend/z.py"]) == "monorepo"

    def test_plain_monolith_unchanged(self):
        assert self._arch(["app/main.py", "app/util.py", "app/db.py"]) == "monolith"

    def test_library_unchanged(self):
        assert self._arch(["src/lib.py", "tests/test_lib.py"]) == "library"
