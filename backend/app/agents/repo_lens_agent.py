import json
from collections import Counter
from pathlib import Path
from typing import Any, Dict, List

from .base_agent import BaseAgent
from ..schemas.agent_schemas import RepoLensOutput, ArchitectureRisk

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

_NPM_FRAMEWORKS = {
    "react": "React", "vue": "Vue.js", "@angular/core": "Angular",
    "svelte": "Svelte", "next": "Next.js", "nuxt": "Nuxt.js",
    "express": "Express.js", "fastify": "Fastify", "@nestjs/core": "NestJS",
    "vite": "Vite", "prisma": "Prisma", "tailwindcss": "Tailwind CSS",
    "framer-motion": "Framer Motion", "axios": "Axios",
    "jest": "Jest", "vitest": "Vitest", "playwright": "Playwright",
}

_PY_FRAMEWORKS = {
    "fastapi": "FastAPI", "django": "Django", "flask": "Flask",
    "sqlalchemy": "SQLAlchemy", "celery": "Celery", "pydantic": "Pydantic",
    "uvicorn": "Uvicorn", "httpx": "HTTPX", "openai": "OpenAI SDK",
    "anthropic": "Anthropic SDK", "pytest": "Pytest", "alembic": "Alembic",
    "langchain": "LangChain",
}

_IGNORE_EXTS = {".md", ".txt", ".json", ".yml", ".yaml", ".toml",
               ".lock", ".css", ".html", ".svg", ".png", ".jpg",
               ".ico", ".woff", ".woff2", ".ttf", ".map"}


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
        score = self._score(has_ci, has_docker, has_tests, risks, tree)

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

    # ── detection helpers ───────────────────────────────────────────────────

    def _detect_stack(self, tree: List[str], kf: Dict[str, str]) -> List[str]:
        stack: set = set()

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

        req = kf.get("requirements.txt", "") + kf.get("pyproject.toml", "")
        if req:
            rl = req.lower()
            for key, label in _PY_FRAMEWORKS.items():
                if key in rl:
                    stack.add(label)
            stack.add("Python")

        if kf.get("go.mod"):
            stack.add("Go")
        if kf.get("Cargo.toml") or kf.get("cargo.toml"):
            stack.add("Rust")
        if any("dockerfile" in f.lower() for f in tree):
            stack.add("Docker")

        exts = Counter(Path(f).suffix.lower() for f in tree if Path(f).suffix)
        for ext, lang in _EXT_TO_LANG.items():
            if exts.get(ext, 0) > 3:
                stack.add(lang)

        return stack

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

    def _architecture(self, tree: List[str]) -> str:
        tops: set = set()
        for f in tree:
            parts = f.split("/")
            if len(parts) > 1:
                tops.add(parts[0])

        if {"packages", "apps", "services", "libs"} & tops:
            return "monorepo"
        if len(tops) > 6 and any(d in tops for d in ["api", "gateway", "service"]):
            return "microservices"
        # Tiered web app: a distinct frontend dir AND a distinct backend dir.
        # ShipMate itself (backend/ + frontend/) is the canonical case — the old
        # classifier fell through to "monolith", misleading every downstream
        # agent about deployment shape (independent FE/BE scaling, cross-layer
        # contract/version-skew risks). Checked before library/monolith.
        if ({"frontend", "web", "ui", "client"} & tops) and \
                ({"backend", "api", "server"} & tops):
            return "tiered_app"
        if "src" in tops and len(tops) < 6:
            return "library"
        return "monolith"

    def _entry_points(self, tree: List[str]) -> List[str]:
        ENTRIES = {
            "main.py", "app.py", "server.py", "run.py", "manage.py",
            "wsgi.py", "asgi.py", "index.ts", "index.js",
            "server.ts", "server.js", "main.ts", "main.js",
            "app.ts", "app.js", "main.go",
        }
        EXCLUDE_DIR_TOKENS = ("/types/", "/__tests__/", "/tests/", "/mocks/",
                              "/mock/", "/fixtures/", "/stubs/", "/__mocks__/",
                              "/.storybook/", "/dist/", "/build/")
        out: List[str] = []
        for f in tree:
            if Path(f).name not in ENTRIES:
                continue
            # `index.ts/js` re-export files in /types/, /__tests__/ etc are
            # NOT real app entry points — they're definitions or mocks. Only
            # accept the entry filename when it's at a meaningful location.
            normalized = "/" + f
            if any(tok in normalized for tok in EXCLUDE_DIR_TOKENS):
                continue
            out.append(f)
            if len(out) >= 8:
                break
        return out

    def _config_files(self, tree: List[str]) -> List[str]:
        CONFIGS = {
            "package.json", "requirements.txt", "pyproject.toml", "setup.py",
            "Dockerfile", "docker-compose.yml", "docker-compose.yaml",
            "tsconfig.json", ".eslintrc.js", ".eslintrc.json", ".env.example",
            "go.mod", "Cargo.toml", "Gemfile", "pom.xml", "Makefile",
            ".gitignore", "nginx.conf",
        }
        found = []
        for f in tree:
            name = Path(f).name
            if name in CONFIGS or ".github/workflows" in f:
                found.append(f)
        return found[:20]

    def _key_modules(self, tree: List[str]) -> List[str]:
        SKIP = {"node_modules", ".git", "__pycache__", "dist", "build",
                ".venv", "venv", ".next", ".pytest_cache", "coverage"}
        counts: Dict[str, int] = {}
        for f in tree:
            parts = f.split("/")
            if len(parts) > 1 and parts[0] not in SKIP:
                counts[parts[0]] = counts.get(parts[0], 0) + 1
        return [d for d, _ in sorted(counts.items(), key=lambda x: -x[1]) if counts[d] > 2][:8]

    def _has_ci_cd(self, tree: List[str]) -> bool:
        markers = [".github/workflows", ".gitlab-ci.yml", "Jenkinsfile",
                   ".circleci/config.yml", "azure-pipelines.yml", ".travis.yml"]
        return any(any(m in f for m in markers) for f in tree)

    def _has_tests(self, tree: List[str]) -> bool:
        for f in tree:
            lo = f.lower()
            if any(t in lo for t in ["test_", "_test.", ".test.", ".spec.", "/tests/", "/test/", "/spec/"]):
                return True
        return False

    # ── risk assessment ────────────────────────────────────────────────────

    def _assess_risks(self, tree, kf, has_ci, has_docker, has_tests) -> List[ArchitectureRisk]:
        risks = []
        if not has_ci:
            risks.append(ArchitectureRisk(risk="No CI/CD pipeline configured", impact="high", category="ci_cd"))
        if not has_docker:
            risks.append(ArchitectureRisk(risk="No Dockerfile — deployment strategy unclear", impact="medium", category="config"))
        if not has_tests:
            risks.append(ArchitectureRisk(risk="No test files detected in repository", impact="high", category="structure"))
        if ".env" in tree:
            risks.append(ArchitectureRisk(risk=".env file committed to repository", impact="critical", category="security"))
        if not any(".gitignore" in f for f in tree):
            risks.append(ArchitectureRisk(risk="No .gitignore — sensitive files unprotected", impact="medium", category="config"))
        if not any("readme" in f.lower() for f in tree):
            risks.append(ArchitectureRisk(risk="No README — project undocumented", impact="low", category="docs"))
        if len(tree) > 1000:
            risks.append(ArchitectureRisk(risk=f"Large codebase ({len(tree)} files) — high complexity", impact="medium", category="structure"))
        return risks

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

    def _score(self, has_ci, has_docker, has_tests, risks, tree) -> int:
        s = 100
        if not has_ci:    s -= 20
        if not has_docker: s -= 8
        if not has_tests:  s -= 20
        if not any("readme" in f.lower() for f in tree): s -= 7
        if ".env" in tree: s -= 15
        if not any(".gitignore" in f for f in tree): s -= 5
        critical = sum(1 for r in risks if r.impact == "critical")
        s -= min(20, critical * 10)
        return max(0, min(100, s))
