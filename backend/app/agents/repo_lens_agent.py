"""RepoLens — deterministic repository analysis agent.

Produces tech stack, architecture pattern, entry points, config files,
engineering risks, and a severity-weighted repo health score. Every output
item is grounded in evidence from the repository file tree and key file
contents. No LLM calls — all logic is heuristic and reproducible.
"""
import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List, Set, Tuple

from .base_agent import BaseAgent
from ..schemas.agent_schemas import RepoLensOutput, ArchitectureRisk

# ── Extension → language map ──────────────────────────────────────────────────

_EXT_TO_LANG = {
    ".py": "Python", ".pyw": "Python",
    ".ts": "TypeScript", ".tsx": "TypeScript",
    ".js": "JavaScript", ".jsx": "JavaScript", ".mjs": "JavaScript",
    ".go": "Go", ".rs": "Rust", ".java": "Java", ".kt": "Kotlin",
    ".swift": "Swift", ".rb": "Ruby", ".php": "PHP",
    ".cs": "C#", ".cpp": "C++", ".cc": "C++", ".c": "C",
    ".scala": "Scala", ".ex": "Elixir", ".dart": "Dart",
    ".r": "R", ".jl": "Julia",
}

# ── npm package → display name ────────────────────────────────────────────────

_NPM_FRAMEWORKS: Dict[str, str] = {
    "react": "React", "vue": "Vue.js", "@angular/core": "Angular",
    "svelte": "Svelte", "next": "Next.js", "nuxt": "Nuxt.js",
    "express": "Express.js", "fastify": "Fastify", "@nestjs/core": "NestJS",
    "vite": "Vite", "prisma": "Prisma", "tailwindcss": "Tailwind CSS",
    "framer-motion": "Framer Motion", "axios": "Axios",
    "jest": "Jest", "vitest": "Vitest", "playwright": "Playwright",
    "socket.io": "Socket.IO", "@supabase/supabase-js": "Supabase",
    "drizzle-orm": "Drizzle ORM", "zod": "Zod",
}

# ── Python package → display name ─────────────────────────────────────────────

_PY_FRAMEWORKS: Dict[str, str] = {
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "sqlalchemy": "SQLAlchemy", "celery": "Celery", "pydantic": "Pydantic",
    "uvicorn": "Uvicorn", "httpx": "HTTPX", "openai": "OpenAI SDK",
    "anthropic": "Anthropic SDK", "pytest": "Pytest", "alembic": "Alembic",
    "langchain": "LangChain", "redis": "Redis",
    "motor": "Motor (MongoDB)", "beanie": "Beanie (MongoDB)",
}

# Extensions to ignore when counting primary language
_IGNORE_EXTS: Set[str] = {
    ".md", ".txt", ".json", ".yml", ".yaml", ".toml",
    ".lock", ".css", ".html", ".svg", ".png", ".jpg",
    ".ico", ".woff", ".woff2", ".ttf", ".map", ".env",
}

# Directories that signal test/utility code — never real entry points
_FALSE_ENTRY_DIRS: Tuple[str, ...] = (
    "/types/", "/type/",
    "/__tests__/", "/tests/", "/test/",
    "/mocks/", "/mock/", "/fixtures/", "/stubs/",
    "/__mocks__/", "/.storybook/",
    "/dist/", "/build/", "/out/", "/.next/",
    "/components/", "/hooks/",
    "/utils/", "/util/",
    "/constants/", "/constant/",
    "/lib/", "/libs/",
    "/store/", "/stores/",
    "/context/", "/contexts/",
    "/assets/", "/static/",
    "/public/",
)

# ── Entry point scoring ───────────────────────────────────────────────────────
# Higher = more likely to be the real application root.

_ENTRY_PRIORITY: Dict[str, int] = {
    # Python backends
    "main.py": 100, "app.py": 90, "server.py": 85, "manage.py": 80,
    "wsgi.py": 75, "asgi.py": 75, "run.py": 70,
    # Go
    "main.go": 100,
    # Node/TS backends
    "server.ts": 85, "server.js": 80,
    # Vite/React/TS frontend mains
    "main.tsx": 90, "main.ts": 85, "main.jsx": 80, "main.js": 75,
    # React root component
    "App.tsx": 70, "App.ts": 65, "App.jsx": 65, "App.js": 60,
    # index files (re-exports common — rank lower)
    "index.tsx": 55, "index.ts": 50, "index.jsx": 50, "index.js": 45,
    # Generic
    "app.ts": 60, "app.js": 55,
}

_ENTRY_NAMES: Set[str] = set(_ENTRY_PRIORITY.keys())

# ── Risk weights (used in _score) ─────────────────────────────────────────────
# One critical >> five lows.  No cap — risks accumulate naturally.

_RISK_WEIGHTS: Dict[str, int] = {
    "critical": 25,
    "high": 18,
    "medium": 8,
    "low": 4,
}

# Top-level dir synonyms for a frontend / backend split (a "fullstack" tree).
# ShipMate itself (backend/ + frontend/) is the canonical case. We accept the
# common synonyms so client/server and web/api layouts classify too, instead of
# falling through to "monolith" and misleading every downstream agent about the
# deployment shape (independent FE/BE scaling, cross-layer version-skew risk).
_FRONTEND_DIRS: Tuple[str, ...] = ("frontend", "web", "ui", "client")
_BACKEND_DIRS: Tuple[str, ...] = ("backend", "api", "server")


# ── Module-level pure helpers ─────────────────────────────────────────────────

def _has_top_level_dir(tree: List[str], name: str) -> bool:
    """True when `name` is a top-level directory in the repo tree."""
    prefix = name + "/"
    return any(f == name or f.startswith(prefix) for f in tree)


def _find_files_by_name(tree: List[str], names: Set[str]) -> List[str]:
    """Return all paths whose basename is in `names`."""
    return [f for f in tree if Path(f).name in names]


def _is_false_entry_point(path: str) -> bool:
    """True when `path` lives inside a directory that doesn't hold real app roots."""
    normalized = "/" + path
    return any(tok in normalized for tok in _FALSE_ENTRY_DIRS)


def _detect_architecture_pattern(tree: List[str]) -> str:
    """Classify the repo layout into a coarse architecture pattern.

    Detection order matters — more specific patterns checked first.
    """
    if len(tree) < 3:
        return "unknown"

    tops: Set[str] = set()
    for f in tree:
        parts = f.split("/")
        if len(parts) > 1:
            tops.add(parts[0])

    # Monorepo — explicit workspace signals
    if {"packages", "apps"} & tops:
        return "monorepo"
    if {"services", "libs"} & tops and len(tops) > 4:
        return "monorepo"

    # Microservices — many top-level dirs including gateway/api/service names
    if len(tops) > 7 and any(d in tops for d in ("api", "gateway", "service", "worker")):
        return "microservices"

    # Fullstack — a distinct frontend layer AND a distinct backend layer
    # (frontend/+backend/, but also client/+server/, web/+api/, ui/+backend/…).
    if (set(_FRONTEND_DIRS) & tops) and (set(_BACKEND_DIRS) & tops):
        return "fullstack"

    # Detect predominant tech signals
    has_py = any(f.endswith(".py") for f in tree)
    has_go = any(f.endswith(".go") for f in tree)
    has_java = any(f.endswith((".java", ".kt", ".scala")) for f in tree)
    has_rust = any(f.endswith(".rs") for f in tree)
    has_server_lang = has_py or has_go or has_java or has_rust

    has_ts = any(f.endswith((".ts", ".tsx")) for f in tree)
    has_jsx = any(f.endswith((".jsx", ".js")) for f in tree)
    has_client_lang = has_ts or has_jsx

    # Only frontend code
    if has_client_lang and not has_server_lang:
        return "frontend"

    # Only backend code
    if has_server_lang and not has_client_lang:
        return "backend"

    # Looks like a publishable library
    if "src" in tops and len(tops) < 5:
        return "library"

    return "monolith"


# ── Agent ─────────────────────────────────────────────────────────────────────

class RepoLensAgent(BaseAgent):
    name = "repo_lens"
    description = "Analyzes repository structure, tech stack, and architecture risks"

    def run(self, context: Dict[str, Any]) -> RepoLensOutput:
        tree = self._file_tree(context)
        kf = self._key_files(context)
        info = self._repo_info(context)

        tech_stack = self._detect_stack(tree, kf)
        primary_lang = self._primary_language(tree, info)
        arch = self._architecture(tree)
        entries = self._entry_points(tree)
        configs = self._config_files(tree)
        modules = self._key_modules(tree)

        has_ci = self._has_ci_cd(tree)
        has_docker = any("dockerfile" in f.lower() for f in tree)
        has_tests = self._has_tests(tree)

        risks = self._assess_risks(tree, kf, has_ci, has_docker, has_tests)
        deps = self._extract_deps(kf)
        score = self._score(risks)

        return RepoLensOutput(
            tech_stack=sorted(tech_stack),
            primary_language=primary_lang,
            architecture_pattern=arch,
            key_modules=modules,
            entry_points=entries,
            config_files=configs,
            has_ci_cd=has_ci,
            has_dockerfile=has_docker,
            has_tests=has_tests,
            architecture_risks=risks,
            dependency_summary=deps,
            file_count=len(tree),
            repo_score=score,
        )

    # ── Architecture ────────────────────────────────────────────────────────

    def _architecture(self, tree: List[str]) -> str:
        return _detect_architecture_pattern(tree)

    # ── Tech stack ──────────────────────────────────────────────────────────

    def _detect_stack(self, tree: List[str], kf: Dict[str, str]) -> List[str]:
        stack: Set[str] = set()

        # ── npm / Node.js ──────────────────────────────────────────────────
        if "package.json" in kf:
            try:
                pkg = json.loads(kf["package.json"])
                deps = {**pkg.get("dependencies", {}), **pkg.get("devDependencies", {})}
                for key, label in _NPM_FRAMEWORKS.items():
                    if any(key in d for d in deps):
                        stack.add(label)
                if deps:
                    stack.add("Node.js")
            except (json.JSONDecodeError, AttributeError):
                pass

        # ── Python ────────────────────────────────────────────────────────
        req = kf.get("requirements.txt", "") + kf.get("pyproject.toml", "")
        if req:
            rl = req.lower()
            for key, label in _PY_FRAMEWORKS.items():
                if key in rl:
                    stack.add(label)
            stack.add("Python")

        # ── Go / Rust / Java ──────────────────────────────────────────────
        if kf.get("go.mod"):
            stack.add("Go")
        if kf.get("Cargo.toml") or kf.get("cargo.toml"):
            stack.add("Rust")
        if any(f.endswith("pom.xml") or f.endswith("build.gradle") for f in tree):
            stack.add("Java")

        # ── Config-file evidence ──────────────────────────────────────────
        if any("tsconfig" in f.lower() for f in tree):
            stack.add("TypeScript")
        if any(Path(f).name.startswith("vite.config") for f in tree):
            stack.add("Vite")
        if any(Path(f).name.startswith("next.config") for f in tree):
            # Next.js already in npm dep scan; catch projects missing package.json read
            stack.add("Next.js")
        if any(Path(f).name.startswith(("tailwind.config", ".tailwindrc")) for f in tree):
            stack.add("Tailwind CSS")
        if any(".github/workflows" in f for f in tree):
            stack.add("GitHub Actions")
        if any("dockerfile" in f.lower() for f in tree):
            stack.add("Docker")
        if any(Path(f).name in ("docker-compose.yml", "docker-compose.yaml") for f in tree):
            stack.add("Docker Compose")

        # ── Extension-count fallback ──────────────────────────────────────
        # Only add a language via extension count if it's NOT already detected
        # from a manifest (avoids double-adding Python when requirements.txt exists).
        exts = Counter(Path(f).suffix.lower() for f in tree if Path(f).suffix)
        for ext, lang in _EXT_TO_LANG.items():
            if exts.get(ext, 0) > 3 and lang not in stack:
                stack.add(lang)

        return stack

    # ── Primary language ────────────────────────────────────────────────────

    def _primary_language(self, tree: List[str], info: Dict) -> str:
        if info.get("language"):
            return info["language"]
        exts = Counter(
            Path(f).suffix.lower() for f in tree
            if Path(f).suffix.lower() not in _IGNORE_EXTS
        )
        if not exts:
            return "Unknown"
        top = exts.most_common(1)[0][0]
        return _EXT_TO_LANG.get(top, top.lstrip(".").capitalize())

    # ── Entry points ────────────────────────────────────────────────────────

    def _entry_points(self, tree: List[str]) -> List[str]:
        """Return ranked list of real application entry files.

        Filters out re-export barrel files (index.ts in /types/, /components/
        etc.) and ranks candidates by how likely they are to be app roots.
        """
        candidates: List[Tuple[int, str]] = []

        for f in tree:
            name = Path(f).name
            if name not in _ENTRY_NAMES:
                continue
            if _is_false_entry_point(f):
                continue
            priority = _ENTRY_PRIORITY.get(name, 40)
            candidates.append((priority, f))

        # Sort descending by priority, then alphabetically for determinism
        candidates.sort(key=lambda x: (-x[0], x[1]))
        return [path for _, path in candidates[:8]]

    # ── Config files ────────────────────────────────────────────────────────

    def _config_files(self, tree: List[str]) -> List[str]:
        CONFIGS = {
            "package.json", "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
            "requirements.txt", "pyproject.toml", "setup.py", "Pipfile", "poetry.lock",
            "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
            "tsconfig.json", "vite.config.ts", "vite.config.js",
            ".eslintrc.js", ".eslintrc.json", ".eslintrc.cjs", "eslint.config.js",
            ".prettierrc", ".prettierrc.json", "biome.json",
            ".env.example", ".env.sample",
            "go.mod", "Cargo.toml", "Gemfile", "pom.xml", "Makefile",
            ".gitignore", "nginx.conf", "ruff.toml", ".flake8",
        }
        found = []
        seen: Set[str] = set()
        for f in tree:
            name = Path(f).name
            if (name in CONFIGS or ".github/workflows" in f) and f not in seen:
                found.append(f)
                seen.add(f)
        return found[:25]

    # ── Key modules ─────────────────────────────────────────────────────────

    def _key_modules(self, tree: List[str]) -> List[str]:
        SKIP = {
            "node_modules", ".git", "__pycache__", "dist", "build",
            ".venv", "venv", ".next", ".pytest_cache", "coverage",
            ".mypy_cache", ".ruff_cache", "out",
        }
        counts: Dict[str, int] = {}
        for f in tree:
            parts = f.split("/")
            if len(parts) > 1 and parts[0] not in SKIP:
                counts[parts[0]] = counts.get(parts[0], 0) + 1
        return [d for d, _ in sorted(counts.items(), key=lambda x: -x[1]) if counts[d] > 2][:8]

    # ── CI/CD detection ──────────────────────────────────────────────────────

    def _has_ci_cd(self, tree: List[str]) -> bool:
        MARKERS = (
            ".github/workflows", ".gitlab-ci.yml", "Jenkinsfile",
            ".circleci/config.yml", "azure-pipelines.yml", ".travis.yml",
            "bitbucket-pipelines.yml", ".drone.yml", "Taskfile.yml",
        )
        return any(any(m in f for m in MARKERS) for f in tree)

    # ── Test detection ───────────────────────────────────────────────────────

    def _has_tests(self, tree: List[str]) -> bool:
        for f in tree:
            lo = f.lower()
            if any(t in lo for t in ("test_", "_test.", ".test.", ".spec.",
                                     "/tests/", "/test/", "/spec/", "/__tests__/")):
                return True
        return False

    # ── Risk assessment ──────────────────────────────────────────────────────

    def _assess_risks(
        self,
        tree: List[str],
        kf: Dict[str, str],
        has_ci: bool,
        has_docker: bool,
        has_tests: bool,
    ) -> List[ArchitectureRisk]:
        """Detect engineering risks with evidence and confidence.

        Each risk is added only when there is deterministic evidence in the
        file tree or key file contents. Low-confidence risks are marked so
        the scoring can weight them appropriately.
        """
        risks: List[ArchitectureRisk] = []

        # ── Security ──────────────────────────────────────────────────────
        if ".env" in tree:
            risks.append(ArchitectureRisk(
                risk=".env file committed to repository",
                impact="critical", category="security",
                evidence=".env",
                confidence="high",
            ))

        # ── CI/CD ────────────────────────────────────────────────────────
        if not has_ci:
            risks.append(ArchitectureRisk(
                risk="No CI/CD pipeline configured",
                impact="high", category="ci_cd",
                evidence=".github/workflows/ not found",
                confidence="high",
            ))

        # ── Testing ───────────────────────────────────────────────────────
        if not has_tests:
            risks.append(ArchitectureRisk(
                risk="No test files detected",
                impact="high", category="structure",
                evidence="No test_*.py, *.test.ts, *.spec.ts found",
                confidence="high",
            ))

        # ── Deployment ────────────────────────────────────────────────────
        # Only flag missing Docker when the repo has a backend (Python/Go/Java)
        has_backend_lang = any(f.endswith((".py", ".go", ".java", ".kt")) for f in tree)
        if not has_docker and has_backend_lang:
            risks.append(ArchitectureRisk(
                risk="No Dockerfile — deployment strategy unclear",
                impact="medium", category="config",
                evidence="Dockerfile not found",
                confidence="high",
            ))

        # ── Version control hygiene ───────────────────────────────────────
        if not any(".gitignore" in f for f in tree):
            risks.append(ArchitectureRisk(
                risk="No .gitignore — sensitive files may be untracked",
                impact="medium", category="config",
                evidence=".gitignore not found",
                confidence="high",
            ))

        # ── Documentation ─────────────────────────────────────────────────
        if not any("readme" in f.lower() for f in tree):
            risks.append(ArchitectureRisk(
                risk="No README — project is undocumented",
                impact="low", category="docs",
                evidence="README.md / README not found",
                confidence="high",
            ))

        # ── Dependency lockfile ───────────────────────────────────────────
        has_lockfile = any(
            Path(f).name in (
                "package-lock.json", "yarn.lock", "pnpm-lock.yaml",
                "poetry.lock", "Pipfile.lock",
            )
            for f in tree
        )
        has_deps_manifest = "package.json" in kf or "requirements.txt" in kf or "Pipfile" in kf
        if not has_lockfile and has_deps_manifest:
            risks.append(ArchitectureRisk(
                risk="No dependency lockfile — builds may not be reproducible",
                impact="medium", category="deps",
                evidence="package-lock.json / yarn.lock / poetry.lock not found",
                confidence="high",
            ))

        # ── Environment documentation ─────────────────────────────────────
        has_env_example = any(
            Path(f).name in (".env.example", ".env.sample", ".env.template")
            for f in tree
        )
        if not has_env_example and has_backend_lang:
            risks.append(ArchitectureRisk(
                risk="No .env.example — environment setup undocumented",
                impact="low", category="docs",
                evidence=".env.example / .env.sample not found",
                confidence="high",
            ))

        # ── TypeScript config ─────────────────────────────────────────────
        has_ts_files = any(f.endswith((".ts", ".tsx")) for f in tree)
        has_tsconfig = any("tsconfig" in Path(f).name.lower() for f in tree)
        if has_ts_files and not has_tsconfig:
            risks.append(ArchitectureRisk(
                risk="TypeScript files present but no tsconfig.json found",
                impact="medium", category="config",
                evidence="*.ts / *.tsx files found; tsconfig.json absent",
                confidence="high",
            ))

        # ── Linting / formatting ──────────────────────────────────────────
        _LINT_MARKERS = (
            ".eslintrc", "eslint.config", "biome.json",
            ".prettierrc", "ruff.toml", ".flake8", "pylintrc",
            ".editorconfig",
        )
        has_lint = any(any(m in f for m in _LINT_MARKERS) for f in tree)
        if not has_lint:
            risks.append(ArchitectureRisk(
                risk="No linting or formatting configuration detected",
                impact="low", category="config",
                evidence=".eslintrc / biome.json / ruff.toml not found",
                # Many tools allow inline config in package.json / pyproject.toml;
                # lower confidence to avoid noise on minimal projects.
                confidence="medium",
            ))

        # ── Codebase size ─────────────────────────────────────────────────
        if len(tree) > 1000:
            risks.append(ArchitectureRisk(
                risk=f"Large codebase ({len(tree)} files) — increased complexity",
                impact="medium", category="structure",
                evidence=f"{len(tree)} files in tree",
                confidence="high",
            ))

        return risks

    # ── Dependency extraction ────────────────────────────────────────────────

    def _extract_deps(self, kf: Dict[str, str]) -> Dict[str, List[str]]:
        """Aggregate dependencies across ALL manifest copies in the repo.

        key_files stores each fetched file under BOTH its basename and its full
        path, and the basename key is last-write-wins — so in a monorepo with
        backend/requirements.txt AND services/x/requirements.txt, the bare
        `kf["requirements.txt"]` holds only one of them. We instead iterate
        every key whose basename matches a manifest, so multi-directory deps are
        all captured, merged, and de-duped (order-preserving)."""
        deps: Dict[str, List[str]] = {}

        def _contents_for(basename: str) -> List[str]:
            # Full-path keys only (contain '/'), so we don't double-count the
            # basename alias of the same file; fall back to the bare key if the
            # repo is flat (manifest at root has no '/'-keyed entry).
            paths = [k for k, v in kf.items() if v and Path(k).name == basename and "/" in k]
            if not paths and basename in kf and kf[basename]:
                paths = [basename]
            return [kf[p] for p in paths]

        def _dedup(seq: List[str]) -> List[str]:
            seen: set = set()
            return [x for x in seq if x and not (x in seen or seen.add(x))]

        npm: List[str] = []
        for content in _contents_for("package.json"):
            try:
                pkg = json.loads(content)
                npm.extend({**pkg.get("dependencies", {}),
                            **pkg.get("devDependencies", {})}.keys())
            except Exception:
                continue
        if npm:
            deps["npm"] = _dedup(npm)[:30]

        py: List[str] = []
        for content in _contents_for("requirements.txt") + _contents_for("requirements-dev.txt"):
            py.extend(
                ln.strip().split("==")[0].split(">=")[0].split("~=")[0].split("[")[0]
                for ln in content.splitlines()
                if ln.strip() and not ln.startswith("#")
            )
        if py:
            deps["python"] = _dedup(py)[:30]

        go: List[str] = []
        for content in _contents_for("go.mod"):
            go.extend(
                ln.split()[1]
                for ln in content.splitlines()
                if ln.startswith("\t") and " v" in ln
            )
        if go:
            deps["go"] = _dedup(go)[:20]

        return deps

    # ── Scoring ─────────────────────────────────────────────────────────────

    def _score(self, risks: List[ArchitectureRisk]) -> int:
        """Severity-weighted deterministic score.

        Starts at 100 and deducts per risk based on impact severity.
        Low-confidence risks (e.g. inferred from absence of tool config)
        are counted at half weight to avoid false punishment.
        """
        s = 100
        for r in risks:
            weight = _RISK_WEIGHTS.get(r.impact, 4)
            # Half-weight for medium-confidence findings
            if getattr(r, "confidence", "high") == "medium":
                weight = weight // 2
            # Zero-weight for low-confidence — informational only
            elif getattr(r, "confidence", "high") == "low":
                weight = 0
            s -= weight
        return max(0, min(100, s))
