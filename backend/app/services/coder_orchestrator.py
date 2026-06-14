"""
CoderOrchestrator — turns an `ActuateRequest` into a real GitHub PR.

Pipeline:
  1. Resolve which file paths Coder should see (heuristic — see _resolve_target_paths).
  2. Fetch their current contents from GitHub (parallel; new files come back empty).
  3. Build a CoderBrief and run CoderAgent (Bedrock, structured output).
  4. Create a fresh branch off the base branch.
  5. Commit each file Coder produced (create or update).
  6. Open a PR against the base branch.
  7. Return ActuateResponse with the PR URL.

Stateless. The caller (the /api/actuate route) gets ALL the context from
the FindingPayload + RepoLensSummary the client passed inline.

If `open_pr=False`, we still create the branch and commit but skip the PR
step — useful for testing without GitHub PR noise.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable, Dict, List, Optional, Tuple

# Optional progress sink: an async callback the streaming route passes in to
# receive per-phase events. None (the default) ⇒ the batch path, unchanged.
EventSink = Optional[Callable[[dict], Awaitable[None]]]

from app.agents.coder_agent import CoderAgent, CoderBrief, CoderFile, CoderOutput
from app.schemas.api_schemas import (
    ActuateRequest, ActuateResponse, ActuatedFile, FindingPayload, RepoLensSummary,
)
from app.services import ast_lint
from app.services import inflight_registry as ir
from app.services import sandbox
from app.services import scope_guard as sg
from app.services import validation_gate as vg
from app.services.github_api_service import GitHubAPIService
from app.services.github_pr_service import GitHubPRService

logger = logging.getLogger("shipmate.coder_orchestrator")

# Whether the pytest gate runs inside the actuate flow. The gate writes to
# the LOCAL working tree (snapshot+restore) — only meaningful when the
# backend runs from a checkout of the repo being actuated (the dogfood
# case). For arbitrary external repos there's nothing local to test against,
# so the gate is a no-op there. Controlled by env so CI / hosted deploys
# can disable it.
_PYTEST_GATE_ENABLED = os.getenv("SHIPMATE_PYTEST_GATE", "1") == "1"

# EvalOps acceptance gate (P5). OFF by default: it boots the patched app in a
# worktree and runs a generated ValidationSpec, which adds latency + an LLM
# call per actuate, so it's opt-in until proven. Fail-open everywhere when on.
_EVAL_GATE_ENABLED = os.getenv("SHIPMATE_EVAL_GATE", "0").strip().lower() in ("1", "true", "yes")

# Hard ceiling on the parallel file-content fetch so a hung GitHub connection
# can't pin the actuate worker thread indefinitely (the gather had no timeout).
_FETCH_TIMEOUT_S = float(os.getenv("SHIPMATE_FETCH_TIMEOUT_S", "45"))

# Hard ceiling per Coder LLM call. Without this a stalled provider response
# pins the worker thread (and, via actuate_stream, the SSE connection) for the
# full request — observed at ~900s when two large full-file generations ran
# concurrently. Set DELIBERATELY BELOW the shared provider read_timeout (300s,
# bedrock_provider.py) so this guard frees the awaiting request/SSE first; the
# orphaned worker thread then drains on the provider's own read-timeout. We do
# NOT lower the provider timeout itself — that client is a process-wide
# singleton shared with the planner/repo_analyst agents, whose legitimate calls
# can run long. Mirrors the _FETCH_TIMEOUT_S guard already used for GitHub I/O.
_LLM_TIMEOUT_S = float(os.getenv("SHIPMATE_LLM_TIMEOUT_S", "240"))

# Auto-engage diff mode when any target file is at least this many lines. Full
# rewrites of large files are slow (timeout risk) and tempt the model to bail
# to `skipped`. Diff mode emits a small unified diff instead, auto-falling back
# to full-file if the diff fails to apply.
_DIFF_AUTO_LINES = int(os.getenv("SHIPMATE_DIFF_AUTO_LINES", "400"))


async def _run_coder(agent: "CoderAgent", *args) -> "CoderOutput":
    """Bound a single Coder call so a hung provider can't pin the request/SSE
    indefinitely. Raises asyncio.TimeoutError on expiry; run_actuation maps it
    to status='timeout'. Note: cancelling the to_thread frees the awaiting
    coroutine but cannot kill the worker thread — the thread drains when the
    provider's own read_timeout (300s, > this deadline) fires. Bounding the
    request promptly is the point; the thread cleans up shortly after."""
    return await asyncio.wait_for(
        asyncio.to_thread(agent.run, *args), _LLM_TIMEOUT_S,
    )


async def _run_coder_feedback(agent, brief, issues, hint) -> "CoderOutput":
    """Bounded re-prompt that feeds concrete issues back to the Coder. Same
    deadline as _run_coder; used by the phantom-patch guard for its one retry."""
    return await asyncio.wait_for(
        asyncio.to_thread(agent.run_with_lint_feedback, brief, issues, hint),
        _LLM_TIMEOUT_S,
    )


# Feedback fed to the Coder when its first response was an empty patch that
# nonetheless claimed a fix (phantom). Names the contradiction explicitly.
_PHANTOM_FEEDBACK = [
    "Your previous response returned ZERO file edits but the summary described "
    "a change as if it had been made. That is a phantom patch and is rejected. "
    "Either emit the ACTUAL complete file content for every file you change, or "
    "— if you genuinely cannot make this change — return no files and set the "
    "summary to exactly 'DECLINED: <one-line reason>'. Do not write a VERIFY "
    "line on an empty patch.",
]


def _looks_like_phantom(coder_out) -> bool:
    """A phantom patch = zero file edits AND a summary that reads like a fix was
    made (carries the mandatory 'VERIFY:' self-check stamp, or is a multi-word
    narration) rather than an honest 'DECLINED:'. An explicit decline is NOT a
    phantom — we only retry the dishonest-looking empties."""
    if getattr(coder_out, "files", None):
        return False
    summary = (getattr(coder_out, "summary", "") or "").strip()
    if not summary:
        return False
    low = summary.lower()
    if low.startswith("declined"):
        return False  # honest decline — accept as-is, don't retry
    # Clean VERIFY stamp on an empty patch is the signature contradiction; a
    # long narration without a decline is also suspect.
    return ("verify:" in low and "fail:" not in low) or len(summary.split()) >= 8


def _honest_empty_summary(coder_out) -> str:
    """Strip a phantom's clean VERIFY stamp from the user-facing summary so an
    empty patch never reports as a clean success. Honest declines pass through."""
    summary = (getattr(coder_out, "summary", "") if coder_out else "") or ""
    summary = summary.strip()
    if not summary:
        return "Coder produced no patch."
    if summary.lower().startswith("declined"):
        return summary
    # Strip the VERIFY stamp wherever it sits (own line OR inline, since the
    # model often appends it to the last sentence) — it would otherwise read as
    # a passed check on an empty patch. Cut from the first 'VERIFY:' onward.
    low = summary.lower()
    idx = low.find("verify:")
    cleaned = (summary[:idx] if idx != -1 else summary).strip()
    cleaned = " ".join(cleaned.split()) or "(no detail)"
    return f"No patch produced (Coder returned no file edits). Coder note: {cleaned[:240]}"

# The repo this backend checkout corresponds to. The pytest gate only fires
# when the actuate target matches — otherwise we'd be running ShipMate's own
# tests against a patch meant for someone else's repo.
_SELF_REPO = os.getenv("SHIPMATE_SELF_REPO", "WalkingDevFlag/Shipmate-AI")

# How aggressive the smart vs fast model routing is.
# Security and CI/CD changes warrant Sonnet; tests/docs are fine on Haiku.
_SMART_KINDS = {"guardrail", "next_action"}
_SMART_CATEGORIES = {"secrets", "auth", "cors", "injection", "config", "ci_cd"}


def _slugify(text: str, max_len: int = 32) -> str:
    s = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return s[:max_len] or "actuate"


def _finding_signature(finding: FindingPayload) -> str:
    """Stable cross-context identifier. MUST match coder_loop._finding_signature
    so the CLI loop and the UI orchestrator agree on which finding is which
    (shared finding_journal + inflight claims). Format: kind::title::file."""
    title = (finding.title or "").strip().lower()[:80]
    file_hint = (finding.file or "").lower()
    return f"{finding.kind}::{title}::{file_hint}"


def _branch_name(finding: FindingPayload) -> str:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    return f"shipmate/{finding.kind}-{_slugify(finding.id)}-{ts}"


def _record_coder_lessons(repo_full: str, gate: str, issues) -> None:
    """Distill a gate rejection into per-repo Coder lessons (B2). Best-effort —
    a lessons-write failure must never affect the actuate's own outcome."""
    try:
        from app.services import coder_lessons as cl
        cl.record_failure(repo_full, gate, issues)
    except Exception as e:  # pragma: no cover - best-effort
        logger.debug("coder_lessons.record_failure skipped (%s)", e)


def _commit_message(finding: FindingPayload, path: str) -> str:
    head = finding.title[:60].rstrip()
    return f"shipmate({finding.kind}): {head} — {path}"


def _pr_title(finding: FindingPayload) -> str:
    icon = {
        "guardrail": "🛡️",
        "milestone": "🚀",
        "blocker": "🚧",
        "test": "🧪",
        "next_action": "✨",
    }.get(finding.kind, "🤖")
    return f"{icon} ShipMate: {finding.title[:80]}"


def _pr_body(req: ActuateRequest, coder: CoderOutput) -> str:
    f = req.finding
    lines: List[str] = [
        f"## ShipMate AI — automated patch",
        "",
        f"**Originating finding:** `{f.kind}` / `{f.id}`"
        + (f" · severity **{f.severity}**" if f.severity else ""),
        f"**Title:** {f.title}",
        "",
        "### Description",
        f.description or "_(none)_",
        "",
    ]
    if f.recommendation:
        lines += ["### ShipMate recommendation", f.recommendation, ""]
    lines += ["### Coder summary", coder.summary or "_(no summary returned)_", ""]
    if coder.files:
        lines += ["### Files changed"]
        for cf in coder.files:
            lines.append(f"- `{cf.path}` — {cf.rationale}")
        lines.append("")
    if coder.skipped:
        lines += ["### Skipped (no change needed)"]
        for p in coder.skipped:
            lines.append(f"- `{p}`")
        lines.append("")
    lines += [
        "---",
        "_This PR was generated by ShipMate AI's Coder agent. Please review carefully before merging._",
        f"_Run id: {datetime.now(timezone.utc).isoformat()} · base: `{req.branch}`_",
    ]
    return "\n".join(lines)


# ── Target-path heuristics ───────────────────────────────────────────────────

_BACKEND_HINT_NAMES = ("main.py", "app.py", "server.py", "index.ts", "index.js", "main.ts")


def _resolve_target_paths(
    finding: FindingPayload,
    context: Optional[RepoLensSummary],
    file_tree: List[str],
) -> List[str]:
    """
    Pick up to 5 paths to show Coder. Empty strings are filtered. New (non-existent)
    paths are allowed — the orchestrator treats them as "create file".
    """
    paths: List[str] = []
    tree_set = set(file_tree)
    entry_points = list(context.entry_points) if context else []

    # 1. Explicit hint from the finding (GuardRail/file or test target).
    if finding.file:
        paths.append(finding.file)

    cat = (finding.category or "").lower()
    kind = finding.kind

    # 2. Category-based mapping.
    if kind == "guardrail":
        if cat in {"secrets", "cors", "auth", "injection", "config"}:
            for p in entry_points[:2]:
                if p and p not in paths:
                    paths.append(p)
            for p in file_tree:
                name = p.rsplit("/", 1)[-1]
                if name in _BACKEND_HINT_NAMES and p not in paths:
                    paths.append(p)
                    if len(paths) >= 4:
                        break
            if cat == "secrets" and ".gitignore" in tree_set and ".gitignore" not in paths:
                paths.append(".gitignore")
        elif cat == "deps":
            for p in ("requirements.txt", "package.json", "pyproject.toml"):
                if p in tree_set and p not in paths:
                    paths.append(p)

    elif kind in {"milestone", "blocker", "next_action"}:
        if cat == "ci_cd":
            ci_path = ".github/workflows/ci.yml"
            paths.append(ci_path)
        elif cat == "testing":
            entry = entry_points[0] if entry_points else ""
            stem = entry.rsplit("/", 1)[-1].rsplit(".", 1)[0] if entry else "main"
            paths.append(f"tests/test_{stem or 'main'}.py")
            if entry and entry not in paths:
                paths.append(entry)
        elif cat in {"feature", "infra", "docs", "security"}:
            for p in entry_points[:2]:
                if p and p not in paths:
                    paths.append(p)

    elif kind == "test":
        # finding.file may already carry SuggestedTest.target_file (the
        # production code under test). If so, KEEP it — Coder needs to see
        # the real implementation to ground assertions. Then add a sibling
        # test file to write into. Also include EXISTING test files for the
        # same module — Coder must see them so it doesn't rewrite tests
        # that are already passing (existing coverage rule #3).
        prod_file = finding.file if finding.file in tree_set else None
        if prod_file:
            stem = prod_file.rsplit("/", 1)[-1].rsplit(".", 1)[0]
            # Find any existing test files that target this module.
            existing = [
                f for f in file_tree
                if (f"test_{stem}" in f.rsplit("/", 1)[-1] or f"{stem}.test." in f.rsplit("/", 1)[-1])
                and ("/tests/" in f or "/__tests__/" in f or "tests/" in f)
            ][:2]
            for ex in existing:
                if ex not in paths:
                    paths.append(ex)
            # Add a target test path (creates new file if not in existing).
            if prod_file.startswith("backend/"):
                target_test = f"backend/tests/test_{stem}.py"
            elif prod_file.startswith("frontend/"):
                ext = prod_file.rsplit(".", 1)[1] if "." in prod_file else "ts"
                target_test = f"frontend/src/__tests__/{stem}.test.{ext}"
            else:
                target_test = f"backend/tests/test_{stem}.py"
            if target_test not in paths and target_test not in existing:
                paths.append(target_test)
        else:
            stem = _slugify(finding.id, max_len=40) or "case"
            paths.append(f"backend/tests/test_{stem}.py")
            # Also include the entry point so Coder has SOMETHING real to import.
            for p in entry_points[:1]:
                if p and p not in paths:
                    paths.append(p)

    # 3. Fallback to top entry point.
    if not paths and entry_points:
        paths.append(entry_points[0])

    # 4. Last-ditch fallback: README so Coder can at least leave a note.
    if not paths:
        paths.append("README.md")

    # Dedupe + soft cap. The cap is on the INPUT context size (how many
    # files we fetch from GitHub and feed Coder), not the output. We keep
    # this generous (15) so Coder has enough context for cross-file
    # changes; it's still bounded so the prompt doesn't blow up token
    # budget on huge target_files blobs. Each file is also independently
    # truncated at 30k chars by the Coder agent's _truncate_for_prompt.
    _CONTEXT_PATH_LIMIT = 15
    seen = set()
    deduped: List[str] = []
    for p in paths:
        if p and p not in seen:
            seen.add(p)
            deduped.append(p)
        if len(deduped) >= _CONTEXT_PATH_LIMIT:
            break
    return deduped


async def _fetch_current_contents(
    token: str, owner: str, repo: str, paths: List[str],
    ref: Optional[str] = None,
) -> Dict[str, str]:
    """
    Best-effort parallel fetch. Missing files → empty string (Coder treats those
    as new-file creations). `ref` should be the analysis target branch — without
    it, GitHub serves the repo's default branch which is almost never what we
    want when analyzing a feature branch.
    """
    sem = asyncio.Semaphore(5)

    async def fetch(path: str) -> Tuple[str, str]:
        async with sem:
            try:
                content = await GitHubAPIService.get_file_content(
                    token, owner, repo, path, ref=ref,
                )
                return path, (content or "")
            except Exception as e:
                logger.warning("Could not fetch %s: %s — treating as new file", path, e)
                return path, ""

    # Overall timeout guard: without it a single hung GitHub connection pins this
    # gather (and the worker thread behind the actuate call) indefinitely. On
    # timeout we degrade to empty contents — Coder treats missing files as
    # new-file creations, same as the per-file except above.
    try:
        pairs = await asyncio.wait_for(
            asyncio.gather(*[fetch(p) for p in paths]),
            timeout=_FETCH_TIMEOUT_S,
        )
    except asyncio.TimeoutError:
        logger.warning(
            "fetch_current_contents timed out after %ss for %s/%s (%d paths) — "
            "proceeding with empty contents", _FETCH_TIMEOUT_S, owner, repo, len(paths),
        )
        return {p: "" for p in paths}
    return {p: c for p, c in pairs}


def _deployment_hint(finding: FindingPayload) -> str:
    cat = (finding.category or "").lower()
    if finding.kind in _SMART_KINDS or cat in _SMART_CATEGORIES:
        return "smart"
    return "fast"


# ── Post-Coder safety lint ──────────────────────────────────────────────────

_NEW_TEST_THEATER_PATTERNS = (
    re.compile(r"status_code\s+in\s*[\(\[][^)\]]*4\d\d", re.MULTILINE),
    re.compile(r"^\s*assert\s+True\s*$", re.MULTILINE),
)

# Dynamic-execution sinks the inbound sanitize middleware blocks. We reject a
# patch that INTRODUCES one (vs the original). The negative-lookbehind avoids
# matching method calls like `ast.literal_eval(` / `self.exec(`.
_CODER_INJECTION_PATTERNS = (
    re.compile(r"(?<![\w.])eval\s*\("),
    re.compile(r"(?<![\w.])exec\s*\("),
    re.compile(r"(?<![\w.])__import__\s*\("),
    re.compile(r"\bos\.system\s*\("),
    re.compile(r"\bsubprocess\.(?:call|run|Popen)\s*\([^)]*shell\s*=\s*True"),
)


def _looks_like_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1]
    return (
        name.startswith("test_")
        or name.endswith((".test.tsx", ".test.ts", ".spec.ts", ".spec.tsx"))
        or "/tests/" in path
        or "/__tests__/" in path
    )


def _lint_coder_output(
    coder_out: CoderOutput,
    target_files: Dict[str, str],
    file_tree: List[str],
) -> List[str]:
    """
    Returns a list of structural issues (empty list = pass). The orchestrator
    rejects the patch (no branch/PR) if this returns anything non-empty.

    Import / symbol checks are delegated to `ast_lint` (real AST parsing) —
    that module replaced the brittle regex detectors that false-positived on
    aliased / TYPE_CHECKING / try-ImportError imports. Test-theater and
    .gitkeep-theater checks stay here (they're cheap regex on text and don't
    benefit from AST).
    """
    issues: List[str] = []
    # Files created IN THIS patch are valid import targets even though they
    # are not in the existing GitHub tree yet. Without this, the textbook
    # "extract into a shared module" refactor (create app/services/sse.py AND
    # `from app.services.sse import …` in the same patch) is wrongly rejected
    # as a hallucinated import. The stale-named check below already trusts
    # coder_out.files; the existence check (detect_bad_imports) must too.
    tree_with_patch = list(file_tree) + [cf.path for cf in coder_out.files]
    for cf in coder_out.files:
        original = target_files.get(cf.path, "")

        if cf.path.endswith(".py"):
            # Fatal: unparseable Python. No point shipping a file that won't import.
            syntax_err = ast_lint.syntax_error_of(cf.new_content, cf.path)
            if syntax_err:
                issues.append(syntax_err)
                continue  # skip further checks — they'd just re-report the parse failure

            bad = ast_lint.detect_bad_imports(cf.new_content, original, tree_with_patch)
            if bad:
                issues.append(
                    f"{cf.path}: hallucinated first-party imports not in repo or "
                    f"original file: {bad}"
                )
            stale_named = ast_lint.detect_stale_named_imports(
                cf.new_content, target_files, coder_out.files,
            )
            if stale_named:
                issues.append(
                    f"{cf.path}: imports symbols that don't exist in the "
                    f"target module(s): {stale_named[:5]}"
                )

        # NEW test theater = patterns Coder added that weren't in the original.
        if _looks_like_test_path(cf.path):
            for pat in _NEW_TEST_THEATER_PATTERNS:
                if pat.search(cf.new_content) and not pat.search(original):
                    issues.append(
                        f"{cf.path}: introduces test theater pattern "
                        f"`{pat.pattern[:50]}` (assertions accepting 4xx as success "
                        f"or `assert True`)"
                    )

        # Only flag .gitkeep inside dirs the codebase normally ignores —
        # bootstrapping an empty `nginx/certs/` or `data/` is legitimate.
        if re.search(
            r"(__pycache__|/build|/dist|/\.venv|/node_modules)/[^/]*\.gitkeep$",
            cf.path,
        ):
            issues.append(
                f"{cf.path}: .gitkeep inside an ignored/build directory "
                "(theater pattern — preserves a dir the patch claims to remove)"
            )

        # Back-door symmetry: the Coder must not INTRODUCE a dynamic-execution
        # sink that the inbound sanitize middleware blocks. Flag a real
        # eval/exec/__import__ CALL the patch ADDS that wasn't in the original
        # (pre-existing ones aren't this patch's fault). Closes the asymmetry
        # where the front door rejects eval( but our own generator could write it.
        if cf.path.endswith(".py"):
            for pat in _CODER_INJECTION_PATTERNS:
                if pat.search(cf.new_content) and not pat.search(original):
                    issues.append(
                        f"{cf.path}: patch introduces a dynamic-execution call "
                        f"(`{pat.pattern}`) not present in the original — the same "
                        "pattern the inbound sanitizer blocks. Use a safe alternative."
                    )
    return issues


def _build_repo_map(
    file_tree: List[str],
    target_files: Dict[str, str],
    target_paths: List[str],
) -> str:
    """Build the Coder repo map (real modules + exported symbols) for the brief.
    Fail-open: any error returns "" so a malformed tree can never break an
    actuate — the Coder just runs without the map section, as it did before."""
    try:
        from app.services import repo_map as rm
        return rm.build_repo_map(file_tree or [], target_files or {}, target_paths or [])
    except Exception as e:  # pragma: no cover - fail-open
        logger.debug("repo_map build skipped (%s)", e)
        return ""


def _build_task(finding: FindingPayload, repo_full_name: str = "") -> str:
    """The instruction string that becomes the Coder agent's `task`.

    When `repo_full_name` is given, appends the repo's recurring Coder lessons
    (B2 failure-memory) so the model is told exactly which past mistakes to
    avoid for THIS repo (hallucinated imports, scope drift, test breakage, …)
    instead of re-making them. Fail-open: a lessons lookup error just omits the
    block."""
    pieces = [f"[{finding.kind.upper()}] {finding.title}"]
    if finding.description:
        pieces.append(f"Description: {finding.description}")
    if finding.recommendation:
        pieces.append(f"Recommended approach: {finding.recommendation}")
    pieces.append(
        "Apply the minimal, focused patch needed to address this. "
        "Don't refactor surrounding code or introduce unrelated changes."
    )
    if repo_full_name:
        try:
            from app.services import coder_lessons as cl
            digest = cl.lessons_digest(repo_full_name)
            if digest:
                pieces.append(digest)
        except Exception as e:  # pragma: no cover - best-effort
            logger.debug("lessons_digest injection skipped (%s)", e)
    return "\n\n".join(pieces)


# ── Top-level orchestrator ───────────────────────────────────────────────────

class CoderOrchestrator:

    @classmethod
    async def _run_decomposed(
        cls,
        req: "ActuateRequest",
        ctx: RepoLensSummary,
        file_tree: List[str],
        target_files: Dict[str, str],
        mode: str,
    ) -> CoderOutput:
        """Tier-2 decompose path: plan ordered steps, run Coder once per step,
        and MERGE the steps into a single CoderOutput. Each step sees prior
        steps' new files folded into its target_files (so step N can import
        symbols step N-1 defined). Returns the merged output; all the normal
        gates downstream (lint → scope → pytest → branch → PR) then operate on
        the union, and everything lands in ONE branch / ONE PR.

        Falls back to a single full-finding run if planning yields one step."""
        from app.agents.decomposer import Decomposer

        plan = await asyncio.to_thread(
            Decomposer().plan,
            _build_task(req.finding, f"{req.owner}/{req.repo}"),
            f"{req.owner}/{req.repo}",
            ctx.primary_language,
            ctx.tech_stack,
            file_tree,
        )
        logger.info(
            "decompose: %d step(s) for %s/%s",
            len(plan.steps), req.finding.kind, req.finding.id,
        )

        # Fetch the CURRENT content of every path any step declares but that
        # the finding's own target resolution missed. Without this, a step
        # editing e.g. analysis.py gets an EMPTY original and blind-rewrites
        # it — silently dropping existing code (e.g. an auth guard) that the
        # scope guard then can't catch because it has no original to diff
        # against. We mutate `target_files` IN PLACE so the caller's downstream
        # scope guard sees these originals too. (Observed live: an SSE
        # milestone resolved to README.md only, then decomposed into edits of
        # analysis.py/orchestrator.py and dropped _verify_repo_write_access.)
        declared = {p for step in plan.steps for p in step.target_paths}
        missing = [p for p in declared if p not in target_files]
        if missing:
            fetched = await _fetch_current_contents(
                req.access_token, req.owner, req.repo, missing, ref=req.branch,
            )
            for p, content in fetched.items():
                target_files.setdefault(p, content)
            logger.info("decompose: fetched %d step-path original(s): %s",
                        len(missing), missing)

        agent = CoderAgent()
        # Accumulated file contents, seeded with the originally-fetched files.
        # path -> latest content. Later steps see earlier steps' output.
        merged: Dict[str, CoderFile] = {}
        working_files: Dict[str, str] = dict(target_files)
        hint = _deployment_hint(req.finding)

        for idx, step in enumerate(plan.steps):
            # Each step's target_files = its declared paths (current content
            # from working_files if present, else empty=new) UNION every file
            # produced so far (ground truth for cross-step references).
            step_targets: Dict[str, str] = {}
            for p in step.target_paths:
                step_targets[p] = working_files.get(p, "")
            for p, cf in merged.items():
                step_targets[p] = cf.new_content

            step_brief = CoderBrief(
                task=f"[Step {idx + 1}/{len(plan.steps)}: {step.name}] {step.task}",
                repo_full_name=f"{req.owner}/{req.repo}",
                primary_language=ctx.primary_language,
                tech_stack=ctx.tech_stack,
                entry_points=ctx.entry_points,
                target_files=step_targets,
                # Map built from working_files (originals + prior steps' output),
                # so step N sees the REAL symbols step N-1 just defined — the
                # same ground truth the merged target_files carry.
                repo_map=_build_repo_map(
                    file_tree, working_files, list(step.target_paths),
                ),
                finding_kind=req.finding.kind,
                finding_id=f"{req.finding.id}-step{idx + 1}",
                finding_severity=req.finding.severity,
                finding_category=getattr(req.finding, "category", "") or "",
            )
            # Bounded per step — a hung step raises asyncio.TimeoutError, which
            # propagates to run_actuation's timeout handler (status='timeout').
            step_out: CoderOutput = await _run_coder(
                agent, step_brief, hint, mode,
            )
            for cf in step_out.files:
                merged[cf.path] = cf
                working_files[cf.path] = cf.new_content

        summary = plan.summary or (req.finding.title or "decomposed feature")
        summary = f"{summary} ({len(plan.steps)} steps, {len(merged)} files)"
        return CoderOutput(
            files=list(merged.values()),
            skipped=[],
            summary=summary,
        )

    @classmethod
    async def _run_verify_loop(
        cls, req, brief, agent, coder_out, target_files, file_tree, emit,
    ):
        """Run the inner verify-loop (Phase 4) over the Coder's first output.

        Composes the cheap checks (lint + scope + sandbox smoke) and a feedback
        callback that re-prompts the Coder with the skill-composed prompt, then
        hands both to verify_loop.run_verify_loop. The smoke check executes in a
        throwaway worktree, so it's safe to run before path claims. Returns
        (final_coder_out, VerifyLoopResult).

        Fail-open is DEFENSIVE, not permissive: if the loop can't run, we still
        evaluate the cheap PURE checks (lint + scope, no execution) once so a
        hallucinated-import / scope-drift patch is never shipped unlinted —
        only the EXECUTION step (smoke) and the iteration are skipped."""
        from app.services import verify_checks
        from app.services import verify_loop

        repo_full = f"{req.owner}/{req.repo}"
        hint = _deployment_hint(req.finding)
        kind = req.finding.kind
        category = getattr(req.finding, "category", "") or ""

        def _pure_only_result():
            """Fallback: run lint + scope (no execution) ONCE and report their
            real verdict — so a setup/loop error degrades to the pre-Phase-4
            single-pass lint+scope behaviour, NOT to shipping unverified code."""
            try:
                pure = verify_checks.default_checks(
                    kind, category, target_files, file_tree, include_smoke=False,
                )
                agg = verify_loop._run_checks(coder_out, pure)
                return verify_loop.VerifyLoopResult(
                    coder_out, not agg.issues, 0, list(agg.issues),
                    "clean" if not agg.issues else "setup_error", [],
                    [n for n in agg.name.split(",") if n],
                )
            except Exception as e:  # pragma: no cover - last-resort fail-open
                logger.debug("verify-loop pure fallback failed (%s)", e)
                return verify_loop.VerifyLoopResult(coder_out, True, 0, [], "clean")

        # The smoke (execution) step is only meaningful on ShipMate's own
        # checkout AND when the pytest gate is enabled — it shares the gate's
        # kill-switch so a hosted deploy that sets SHIPMATE_PYTEST_GATE=0 (per
        # the runbook) doesn't silently re-introduce execution here. For an
        # external repo there's no local app.main to import. Either way the
        # pure lint+scope checks still run.
        include_smoke = repo_full == _SELF_REPO and _PYTEST_GATE_ENABLED

        try:
            checks = verify_checks.default_checks(
                kind, category, target_files, file_tree,
                include_smoke=include_smoke,
            )
        except Exception as e:  # pragma: no cover - fail-open to pure checks
            logger.debug("verify-loop check build failed (%s) — pure-check fallback", e)
            return coder_out, _pure_only_result()

        def _feedback(issues):
            # Re-prompt the Coder with the concrete issues. Synchronous (the
            # loop runs on a worker thread via asyncio.to_thread below).
            try:
                return agent.run_with_lint_feedback(brief, issues, hint)
            except Exception as e:
                logger.warning("verify-loop feedback re-prompt raised %s", e)
                return None

        # Secondary bound: a soft approx-token ceiling so the loop can't keep
        # re-prompting deep into a token-heavy analyze/actuate run. Read from
        # env (0/unset ⇒ iteration cap only, the conservative default). When set,
        # the loop stops re-prompting once the active run's spent tokens leave
        # less than the per-Coder-call headroom.
        token_budget = None
        try:
            _tb = int(os.getenv("SHIPMATE_VERIFY_TOKEN_BUDGET", "0") or "0")
            token_budget = _tb if _tb > 0 else None
        except ValueError:
            token_budget = None

        # The loop calls the (blocking) Coder + subprocess smoke, so run the
        # whole thing on a worker thread to keep the event loop free.
        def _drive():
            return verify_loop.run_verify_loop(
                coder_out, checks, _feedback, token_budget=token_budget,
            )

        try:
            # The loop re-prompts the Coder up to verify_loop._DEFAULT_MAX_ITERS
            # times; bound the whole drive so a hung re-prompt can't pin the
            # request past a few per-call deadlines. On timeout, degrade to the
            # pure lint+scope verdict on the candidate we already have rather
            # than hanging — same fail-open as the exception path below.
            loop = await asyncio.wait_for(
                asyncio.to_thread(_drive),
                _LLM_TIMEOUT_S * (verify_loop._DEFAULT_MAX_ITERS + 1),
            )
        except asyncio.TimeoutError:
            logger.warning(
                "verify-loop exceeded its deadline — pure-check fallback on the "
                "candidate in hand",
            )
            return coder_out, _pure_only_result()
        except Exception as e:  # pragma: no cover - fail-open to pure checks
            logger.warning("verify-loop raised %s — pure-check fallback", e)
            return coder_out, _pure_only_result()

        await emit("verify-loop.done", iterations=loop.iterations,
                   passed=loop.passed, stop_reason=loop.stop_reason)
        logger.info(
            "verify-loop %s/%s: %s after %d iter(s) (%s)",
            req.finding.kind, req.finding.id,
            "passed" if loop.passed else "failed",
            loop.iterations, loop.stop_reason,
        )
        return loop.coder_out, loop

    @classmethod
    async def run_actuation(
        cls, req: ActuateRequest, on_event: EventSink = None,
    ) -> ActuateResponse:
        """Run the full actuate pipeline. When `on_event` is supplied (by the
        SSE route), it's awaited at each phase boundary with a small dict so the
        UI sees live progress; otherwise this is the unchanged batch path. The
        SAME gates (lint → scope → pytest → resolution → PR) run in both modes —
        streaming is purely observational, never a second code path."""
        ctx = req.context or RepoLensSummary()
        branch_name = _branch_name(req.finding)

        async def _emit(stage: str, **fields) -> None:
            if on_event is None:
                return
            try:
                await on_event({"event": stage, **fields})
            except Exception as e:  # never let a slow/broken client break actuate
                logger.debug("on_event(%s) raised %s; continuing", stage, e)

        await _emit("actuate.start", finding_id=req.finding.id,
                    kind=req.finding.kind, title=req.finding.title)

        # 1. Get the file tree once so target resolution can probe it.
        await _emit("resolve.start")
        try:
            file_tree = await GitHubAPIService.get_file_tree(
                req.access_token, req.owner, req.repo, req.branch,
            )
        except Exception as e:
            logger.warning("file_tree fetch failed: %s — proceeding without it", e)
            file_tree = []

        target_paths = _resolve_target_paths(req.finding, ctx, file_tree)
        logger.info("Resolved %d target paths for %s/%s: %s",
                    len(target_paths), req.finding.kind, req.finding.id, target_paths)

        # 2. Fetch current contents — pinned to the same `req.branch` we
        # resolved file_tree against. Without this, GitHub serves the
        # default branch and Coder is given a stale picture, leading to
        # patches that re-introduce imports/symbols already removed.
        target_files = await _fetch_current_contents(
            req.access_token, req.owner, req.repo, target_paths,
            ref=req.branch,
        )
        await _emit("resolve.done", paths=target_paths)

        # 3. Run Coder (sync, on a worker thread).
        await _emit("coding.start", paths=target_paths)
        brief = CoderBrief(
            task=_build_task(req.finding, f"{req.owner}/{req.repo}"),
            repo_full_name=f"{req.owner}/{req.repo}",
            primary_language=ctx.primary_language,
            tech_stack=ctx.tech_stack,
            entry_points=ctx.entry_points,
            target_files=target_files,
            # Real module/symbol map so Coder imports only what exists — the
            # prevention half of the hallucinated-import defense ast_lint cures.
            repo_map=_build_repo_map(file_tree, target_files, target_paths),
            finding_kind=req.finding.kind,
            finding_id=req.finding.id,
            finding_severity=req.finding.severity,
            finding_category=getattr(req.finding, "category", "") or "",
        )
        agent = CoderAgent()
        # Diff mode when explicitly requested OR auto-engaged for large target
        # files: reprinting a 1200-line file verbatim to add a 3-line change is
        # what blows the LLM timeout and tempts the model to bail to `skipped`.
        # CoderAgent auto-falls-back to full-file on any diff-apply failure, so
        # this only ever saves work — it never blocks a patch.
        _auto_diff = any(
            (c or "").count("\n") + 1 > _DIFF_AUTO_LINES
            for c in target_files.values()
        )
        _mode = "diff" if (getattr(req, "diff_mode", False) or _auto_diff) else "full"
        if _auto_diff and not getattr(req, "diff_mode", False):
            logger.info(
                "auto-engaging diff mode for %s/%s: a target file exceeds %d lines",
                req.finding.kind, req.finding.id, _DIFF_AUTO_LINES,
            )
        try:
            if getattr(req, "decompose", False):
                coder_out = await cls._run_decomposed(
                    req, ctx, file_tree, target_files, _mode,
                )
            else:
                coder_out = await _run_coder(
                    agent, brief, _deployment_hint(req.finding), _mode,
                )
        except asyncio.TimeoutError:
            logger.warning(
                "Coder call exceeded %.0fs for %s/%s — returning timeout",
                _LLM_TIMEOUT_S, req.finding.kind, req.finding.id,
            )
            await _emit("done", status="timeout", pr_url=None)
            return ActuateResponse(
                status="timeout",
                pr_url=None,
                branch_name=branch_name,
                files_changed=[],
                skipped=target_paths,
                summary=(
                    f"Coder call exceeded the {_LLM_TIMEOUT_S:.0f}s deadline "
                    f"(SHIPMATE_LLM_TIMEOUT_S) and was aborted. No patch produced."
                ),
            )
        except Exception as e:
            # A malformed model response (e.g. Bedrock returns a CoderOutput
            # missing a required field even after the provider's array-not-string
            # retry) raises here. That's a flaky-LLM outcome, NOT a server fault —
            # fail SOFT to a coder_error status (like timeout) instead of letting
            # it surface as a 500. Callers already treat any status != "complete"
            # as a non-shipping outcome, so this slots in cleanly.
            logger.warning(
                "Coder call failed for %s/%s (%s: %s) — returning coder_error",
                req.finding.kind, req.finding.id, type(e).__name__, str(e)[:200],
            )
            _record_coder_lessons(
                f"{req.owner}/{req.repo}", "coder_error", [f"{type(e).__name__}: {str(e)[:160]}"]
            )
            await _emit("done", status="coder_error", pr_url=None)
            return ActuateResponse(
                status="coder_error",
                pr_url=None,
                branch_name=branch_name,
                files_changed=[],
                skipped=target_paths,
                summary=(
                    f"Coder produced an unusable response ({type(e).__name__}). "
                    f"No patch was applied. Re-run to retry."
                ),
            )
        await _emit("coding.done", files=[cf.path for cf in coder_out.files])

        if not coder_out.files:
            # Empty patch. Distinguish an HONEST decline from a PHANTOM patch —
            # zero files but a confident VERIFY-clean summary that narrates a
            # fix it never emitted (observed live on a cross-cutting finding).
            # On a phantom, re-prompt ONCE with the contradiction made explicit
            # before accepting no_change; an honest decline is returned as-is.
            if _looks_like_phantom(coder_out):
                logger.warning(
                    "phantom patch for %s/%s (empty files + clean VERIFY) — one retry",
                    req.finding.kind, req.finding.id,
                )
                await _emit("coding.retry", reason="phantom")
                try:
                    coder_out = await _run_coder_feedback(
                        agent, brief, _PHANTOM_FEEDBACK, _deployment_hint(req.finding),
                    )
                except asyncio.TimeoutError:
                    coder_out = None  # fall through to no_change below

            if not coder_out or not coder_out.files:
                _record_coder_lessons(
                    f"{req.owner}/{req.repo}", "phantom",
                    [(getattr(coder_out, "summary", "") or "")[:200]
                     or "empty patch"],
                )
                await _emit("done", status="no_change", pr_url=None)
                return ActuateResponse(
                    status="no_change",
                    pr_url=None,
                    branch_name=branch_name,
                    files_changed=[],
                    skipped=(getattr(coder_out, "skipped", None) or target_paths),
                    summary=_honest_empty_summary(coder_out),
                )

        # 3b. Inner verify-loop (Phase 4) — iterate the Coder against the CHEAP
        # checks (AST lint + scope guard + sandbox import-smoke) BEFORE touching
        # GitHub or the expensive full pytest gate. This unifies what used to be
        # three scattered one-shot feedback retries (lint, scope) into one
        # bounded loop: generate → cheap-verify → feed concrete errors back →
        # repeat, ≤ max_iters and ≤ token budget. Each re-prompt uses the
        # skill-composed prompt (P3); the smoke runs in a throwaway worktree
        # (P2, race-free); the brief carries the repo map (P1).
        await _emit("gate.start", phase="verify-loop")
        repo_full = f"{req.owner}/{req.repo}"
        coder_out, loop = await cls._run_verify_loop(
            req, brief, agent, coder_out, target_files, file_tree, _emit,
        )

        if not coder_out.files or not loop.passed:
            outstanding = loop.issues or ["Coder produced no usable files"]
            logger.warning(
                "verify-loop did not converge for %s/%s after %d iter(s) (%s): %s",
                req.finding.kind, req.finding.id, loop.iterations,
                loop.stop_reason, outstanding,
            )
            # Classify by WHICH check failed (the loop tags each), not by
            # sniffing issue text — scope_guard's catch-all "DELETES N of M
            # lines" message carries none of the old keyword markers. A scope
            # failure surfaces as scope_rejected, everything else lint_rejected.
            scope_failed = "scope" in (loop.failed_checks or [])
            # Failure-memory (B2): record under the gate that actually failed so
            # the lessons signal isn't flattened to always-"lint".
            _record_coder_lessons(
                repo_full, "scope" if scope_failed else "lint", outstanding,
            )
            status = "scope_rejected" if scope_failed else "lint_rejected"
            await _emit("done", status=status, pr_url=None)
            return ActuateResponse(
                status=status,
                pr_url=None,
                branch_name=branch_name,
                files_changed=[],
                skipped=[cf.path for cf in coder_out.files],
                summary=(
                    f"Coder output rejected by the verify-loop after "
                    f"{loop.iterations} feedback iteration(s) "
                    f"({loop.stop_reason}): {'; '.join(outstanding) or 'no files'}. "
                    f"No branch/PR was created. "
                    f"Coder summary: {coder_out.summary[:300]}"
                ),
            )

        # 3c. Path claim — atomic cross-process lock so a UI click and the
        # CLI loop (or two UI clicks) can't both rewrite the same file. If
        # any path is already claimed, bail with `path_busy` immediately
        # (user-facing retry — see ActuateButton). Always released in `finally`.
        # (repo_full was set above for the verify-loop.)
        claimed_paths: List[str] = []
        claimer = f"actuate:{req.finding.kind}:{req.finding.id}"
        sig = _finding_signature(req.finding)
        try:
            for cf in coder_out.files:
                if not ir.claim_path(repo_full, req.branch, cf.path, sig, claimer):
                    holder = ir.get_path_claim(repo_full, req.branch, cf.path)
                    held_by = (holder or {}).get("claimed_by", "another actuate")
                    logger.info("path_busy: %s held by %s", cf.path, held_by)
                    await _emit("done", status="path_busy", pr_url=None)
                    return ActuateResponse(
                        status="path_busy",
                        pr_url=None,
                        branch_name=branch_name,
                        files_changed=[],
                        skipped=[cf.path for cf in coder_out.files],
                        summary=(
                            f"Another actuate is currently editing `{cf.path}` "
                            f"(held by {held_by}). Retry once it finishes."
                        ),
                    )
                claimed_paths.append(cf.path)

            # Serialize the verify-loop's surviving output for the downstream
            # pytest gate / commit. Lint + scope + smoke already passed inside
            # the loop (3b); the path claim above is the first GitHub-touching
            # step, taken on the loop's FINAL file set.
            serialized = [
                {"path": cf.path, "new_content": cf.new_content,
                 "rationale": cf.rationale}
                for cf in coder_out.files
            ]

            # 3d. Local pytest gate — only when actuating THIS repo on a local
            # checkout (the dogfood case). For external repos there's no local
            # tree to test against, so we skip. Snapshot → apply → smoke import
            # → pytest → restore-on-regression. If the patch drops the pass
            # count, reject with `pytest_rejected` and DON'T open a PR.
            # Per-finding gate tier (Phase 2): a docs-only edit doesn't warrant
            # a full ~45s pytest run, while a security patch does. gate_for is
            # deterministic (no LLM) and fail-safe (unknown → full). A
            # lint/import-smoke tier downgrades the HEAVY pytest gate to a
            # cheaper check; every other tier runs the full gate below.
            try:
                _tier = sandbox.gate_for(
                    req.finding.kind, getattr(req.finding, "category", "") or "",
                )
            except Exception:
                _tier = None
            # Only the lint tier skips ALL execution (pure prose). An
            # import-smoke tier (deps/manifest edits) must still RUN the smoke
            # check — skipping it entirely would let a manifest edit that breaks
            # `import app.main` open a PR with zero validation.
            _lint_only_tier = _tier is not None and not _tier.run_pytest and not _tier.run_smoke
            _smoke_only_tier = _tier is not None and _tier.run_smoke and not _tier.run_pytest

            gate_ran = False
            if _PYTEST_GATE_ENABLED and repo_full == _SELF_REPO and _lint_only_tier:
                logger.info(
                    "pytest gate SKIPPED for %s/%s — tier '%s' (%s)",
                    req.finding.kind, req.finding.id, _tier.name, _tier.description,
                )
                await _emit("gate.skip", phase="pytest", tier=_tier.name)
            elif _PYTEST_GATE_ENABLED and repo_full == _SELF_REPO and _smoke_only_tier:
                # Apply the patch, run ONLY the import smoke, restore. Catches a
                # manifest/dependency edit that breaks the entry-point import
                # without paying for the full suite.
                gate_ran = True
                await _emit("gate.start", phase="import-smoke", tier=_tier.name)
                snap = await asyncio.to_thread(vg.snapshot_files,
                                               [cf["path"] for cf in serialized])
                await asyncio.to_thread(vg.write_files_to_tree, serialized)
                smoke_ok, smoke_err = await asyncio.to_thread(vg.smoke_imports)
                await asyncio.to_thread(vg.restore_snapshot, snap)
                if not smoke_ok:
                    _record_coder_lessons(repo_full, "pytest",
                                          f"import smoke failed: {smoke_err[:200]}")
                    await _emit("done", status="pytest_rejected", pr_url=None)
                    return ActuateResponse(
                        status="pytest_rejected",
                        pr_url=None,
                        branch_name=branch_name,
                        files_changed=[],
                        skipped=[cf.path for cf in coder_out.files],
                        summary=(
                            f"Patch (tier '{_tier.name}') broke the entry-point import: "
                            f"{smoke_err[:200]}. No PR was created. "
                            f"Coder summary: {coder_out.summary[:200]}"
                        ),
                    )
                logger.info("import-smoke gate passed for %s/%s (tier '%s')",
                            req.finding.kind, req.finding.id, _tier.name)
            elif _PYTEST_GATE_ENABLED and repo_full == _SELF_REPO:
                gate_ran = True
                await _emit("gate.start", phase="pytest")
                result, snap = await asyncio.to_thread(vg.gate_patch, serialized)
                if not result.passed:
                    await asyncio.to_thread(vg.restore_snapshot, snap)
                    # Feed the actual test failure back to the Coder and retry
                    # once — a regression is often a one-line miss the model
                    # fixes when shown which tests broke (previously the pytest
                    # gate hard-rejected with no retry).
                    logger.warning(
                        "pytest gate rejected %s/%s: %s — retrying once with feedback",
                        req.finding.kind, req.finding.id, result.reason,
                    )
                    try:
                        coder_out = await _run_coder_feedback(
                            agent, brief,
                            [f"Your patch broke the test suite: {result.reason}. "
                             "Fix the regression while still addressing the finding."],
                            _deployment_hint(req.finding),
                        )
                        serialized = [
                            {"path": cf.path, "new_content": cf.new_content,
                             "rationale": cf.rationale}
                            for cf in coder_out.files
                        ]
                        # Re-lint + re-scope the retry output before re-gating.
                        # An EMPTY retry (model declined under pressure) must NOT
                        # count as a pass — an empty patch trivially has no test
                        # regression, which would otherwise fall through to an
                        # empty 'complete'. Keep the original rejection instead.
                        if not coder_out.files or \
                                _lint_coder_output(coder_out, target_files, file_tree) or \
                                sg.check_patch(serialized, target_files, coder_out.summary):
                            result, snap = (result, snap)  # keep failed result
                        else:
                            result, snap = await asyncio.to_thread(vg.gate_patch, serialized)
                            if result.passed:
                                await asyncio.to_thread(vg.restore_snapshot, snap)
                    except Exception as e:
                        logger.warning("pytest-feedback retry raised %s; keeping rejection", e)
                if not result.passed:
                    await asyncio.to_thread(vg.restore_snapshot, snap)
                    _record_coder_lessons(repo_full, "pytest", result.reason)
                    await _emit("done", status="pytest_rejected", pr_url=None)
                    return ActuateResponse(
                        status="pytest_rejected",
                        pr_url=None,
                        branch_name=branch_name,
                        files_changed=[],
                        skipped=[cf.path for cf in coder_out.files],
                        summary=(
                            f"Patch failed the local pytest gate (incl. one feedback "
                            f"retry): {result.reason}. The working tree was restored "
                            f"and no PR was created. Coder summary: {coder_out.summary[:200]}"
                        ),
                    )
                # Gate passed — restore the tree (GitHub commit is the source of
                # truth; we don't want the local checkout to drift). Bump the
                # baseline so a later actuate can't pass by re-clearing these.
                await asyncio.to_thread(vg.restore_snapshot, snap)
                vg.update_baseline(result.after)
                logger.info(
                    "pytest gate passed for %s/%s (%dp → %dp)",
                    req.finding.kind, req.finding.id, result.before, result.after,
                )

            # 3d-ii. TARGET-REPO gate — for repos that AREN'T ShipMate's own
            # checkout. Clones the user's repo, applies the patch, runs THEIR
            # test command in a sandboxed subprocess. OFF by default
            # (SHIPMATE_TARGET_REPO_GATE=1) because it runs untrusted code.
            # Without it, "Build It" on an external repo opens PRs with zero
            # local validation — this closes that gap when explicitly enabled.
            elif vg.target_repo_gate_enabled() and repo_full != _SELF_REPO:
                gate_ran = True
                await _emit("gate.start", phase="target-pytest")
                tgt = await asyncio.to_thread(
                    vg.gate_patch_target_repo,
                    req.owner, req.repo, req.branch, serialized, req.access_token,
                )
                if not tgt.passed:
                    _record_coder_lessons(repo_full, "pytest", tgt.reason)
                    await _emit("done", status="pytest_rejected", pr_url=None)
                    return ActuateResponse(
                        status="pytest_rejected",
                        pr_url=None,
                        branch_name=branch_name,
                        files_changed=[],
                        skipped=[cf.path for cf in coder_out.files],
                        summary=(
                            f"Patch failed the target-repo test gate: {tgt.reason}. "
                            f"No PR was created. Coder summary: {coder_out.summary[:200]}"
                        ),
                    )
                logger.info(
                    "target-repo gate passed for %s/%s (%df failing)",
                    req.owner, req.repo, tgt.failed,
                )

            # 3e. Fix-resolution check — for detectable finding categories, the
            # patch must actually REMOVE the offending pattern. 'shipped' should
            # mean the issue is gone, not merely that tests still pass. A patch
            # that leaves the pattern in place is rejected (resolution_failed)
            # rather than opening a PR that doesn't fix anything. None/unknown
            # categories fall through to the test/lint gate (fail-open).
            try:
                from app.services import finding_critic as fc
                resolved = fc.is_finding_resolved(
                    getattr(req.finding, "category", "") or req.finding.kind,
                    [cf.new_content for cf in coder_out.files],
                )
            except Exception:
                resolved = None
            if resolved is False:
                logger.warning(
                    "resolution check FAILED for %s/%s: offending pattern still present",
                    req.finding.kind, req.finding.id,
                )
                _record_coder_lessons(
                    repo_full, "resolution",
                    f"{req.finding.category or req.finding.kind}: offending pattern still present",
                )
                await _emit("done", status="resolution_failed", pr_url=None)
                return ActuateResponse(
                    status="resolution_failed",
                    pr_url=None,
                    branch_name=branch_name,
                    files_changed=[],
                    skipped=[cf.path for cf in coder_out.files],
                    summary=(
                        "Patch passed lint/tests but did NOT remove the issue it "
                        "targets (the offending pattern is still present), so no PR "
                        f"was opened. Coder summary: {coder_out.summary[:200]}"
                    ),
                )

            # 3f. EvalOps acceptance gate (P5) — for feature/milestone findings
            # on the dogfood repo, "shipped" should mean "the change WORKS", not
            # just "it linted + passed pytest". Generate a ValidationSpec for the
            # finding and run it against the PATCHED app in a throwaway worktree;
            # reject (eval_rejected) if the scenarios fail. Opt-in via
            # SHIPMATE_EVAL_GATE (default off so the hot actuate path is
            # unchanged until explicitly enabled). Fully fail-open: any eval-infra
            # error or a spec that couldn't run is treated as NO SIGNAL (never
            # blocks a patch), mirroring test_synthesizer's report.ran semantics.
            if (_EVAL_GATE_ENABLED and repo_full == _SELF_REPO
                    and req.finding.kind in ("milestone", "blocker", "next_action")):
                try:
                    from app.services import define_validation, eval_runner
                    from app.services.llm_service import LLMService
                    provider = LLMService.provider()
                    spec = define_validation.define_spec(req.finding, provider)
                    await _emit("gate.start", phase="eval", spec=spec.name)
                    # trusted=True: this is the SELF-repo dogfood patch — the
                    # eval must boot the REAL app (needs the real env to import
                    # providers/stores), and the gate only fires for _SELF_REPO,
                    # so there's no untrusted-code exposure here. (A stripped env
                    # would cripple the boot and could noise up the signal.)
                    report = await asyncio.to_thread(
                        lambda: eval_runner.run_eval_in_worktree(
                            spec, serialized, trusted=True,
                        ),
                    )
                    if report is not None and report.ran and not report.passed:
                        fails = [x for s in report.scenarios for x in s.failures][:3]
                        _record_coder_lessons(
                            repo_full, "eval",
                            f"EvalOps spec '{spec.name}' failed: {'; '.join(fails) or 'see report'}",
                        )
                        await _emit("done", status="eval_rejected", pr_url=None)
                        return ActuateResponse(
                            status="eval_rejected",
                            pr_url=None,
                            branch_name=branch_name,
                            files_changed=[],
                            skipped=[cf.path for cf in coder_out.files],
                            summary=(
                                f"Patch passed lint/tests but FAILED its EvalOps "
                                f"acceptance spec ('{spec.name}'): "
                                f"{'; '.join(fails) or 'scenarios did not pass'}. No PR "
                                f"was opened. Coder summary: {coder_out.summary[:200]}"
                            ),
                        )
                    if report is not None and report.passed:
                        logger.info("eval gate PASSED for %s/%s (spec '%s')",
                                    req.finding.kind, req.finding.id, spec.name)
                except Exception as e:  # pragma: no cover - fail-open
                    logger.debug("eval gate skipped (fail-open): %s", e)

            # Backstop: never create a branch / return 'complete' for an empty
            # file set. A gate retry (e.g. the pytest-feedback re-prompt above)
            # can overwrite coder_out with an empty patch, which would otherwise
            # fall through to an empty branch + a misleading 'complete' with
            # files_changed=[] (observed live when auto-diff tripped the retry).
            # An empty set this late is a no_change, not a success.
            if not coder_out.files:
                logger.warning(
                    "empty file set reached the commit stage for %s/%s "
                    "(likely a gate retry emptied the patch) — returning no_change",
                    req.finding.kind, req.finding.id,
                )
                _record_coder_lessons(
                    repo_full, "phantom", ["empty patch after gate retry"],
                )
                await _emit("done", status="no_change", pr_url=None)
                return ActuateResponse(
                    status="no_change",
                    pr_url=None,
                    branch_name=branch_name,
                    files_changed=[],
                    skipped=target_paths,
                    summary=(
                        "No patch produced — the Coder's gate-retry returned an "
                        "empty file set, so nothing was committed."
                    ),
                )

            # 4. Create the branch.
            await _emit("branch.start", branch=branch_name)
            base_sha = await GitHubPRService.get_branch_sha(
                req.access_token, req.owner, req.repo, req.branch,
            )
            await GitHubPRService.create_branch(
                req.access_token, req.owner, req.repo, branch_name, base_sha,
            )

            # 5. Commit each file (sequential — Contents API needs fresh sha per write).
            committed: List[ActuatedFile] = []
            for cf in coder_out.files:
                existing_sha = await GitHubPRService.get_file_sha(
                    req.access_token, req.owner, req.repo, cf.path, branch_name,
                )
                try:
                    await GitHubPRService.put_file(
                        req.access_token,
                        req.owner,
                        req.repo,
                        cf.path,
                        cf.new_content,
                        _commit_message(req.finding, cf.path),
                        branch_name,
                        sha=existing_sha,
                    )
                except Exception as e:
                    # GitHub returns 404 (not 403!) when the OAuth token lacks the
                    # `workflow` scope and the file lives under .github/workflows/.
                    # Surface a clean, actionable error instead of a bare 404.
                    if cf.path.startswith(".github/workflows/") and "404" in str(e):
                        raise RuntimeError(
                            f"Cannot write {cf.path}: your GitHub OAuth token is missing "
                            "the `workflow` scope. Sign out and reconnect GitHub from the "
                            "sidebar to re-grant scopes, then retry."
                        ) from e
                    raise
                committed.append(ActuatedFile(path=cf.path, rationale=cf.rationale))
            await _emit("commit.done", files=[c.path for c in committed])

            # 6. Open PR (optional).
            pr_url: Optional[str] = None
            pr_number: Optional[int] = None
            if req.open_pr:
                await _emit("pr.start")
                pr_url, pr_number = await GitHubPRService.create_pull_request(
                    req.access_token,
                    req.owner,
                    req.repo,
                    _pr_title(req.finding),
                    _pr_body(req, coder_out),
                    head=branch_name,
                    base=req.branch,
                )

                # 7. Register the PR with CIWatcher so we can self-heal CI failures.
                #    Lazy import keeps the orchestrator import-light and avoids
                #    a cycle (ci_watcher imports coder_agent).
                try:
                    from app.services.ci_watcher import CIWatcher
                    CIWatcher.register(
                        owner=req.owner,
                        repo=req.repo,
                        pr_number=pr_number,
                        pr_url=pr_url,
                        branch=branch_name,
                        base_branch=req.branch,
                        access_token=req.access_token,
                        finding=req.finding,
                        repo_lens=ctx,
                    )
                except Exception as e:
                    # Watcher registration is best-effort — don't fail the actuate
                    # call if the watcher couldn't start.
                    logger.warning("CIWatcher.register failed: %s — PR opened without auto-fix", e)

                # Journal: this finding now has an open PR in flight.
                try:
                    ir.journal_set_state(
                        sig, repo_full, "in_progress",
                        pr_url=pr_url, bump_attempt=True,
                    )
                except Exception as e:
                    logger.debug("journal_set_state failed (non-fatal): %s", e)

                # Semantic memory (B1): remember this finding's TEXT so a
                # reworded version of the SAME issue is recognized + suppressed
                # on a future analyze (the exact-signature journal can't catch a
                # rephrase). We remember at PR-open as a 'shipped' candidate; if
                # CI later fails and the watcher gives up, the journal state
                # diverges but the semantic suppression of a true duplicate is
                # still the behaviour we want.
                try:
                    from app.services import finding_memory as fm
                    fm.remember(
                        repo_full, sig, req.finding.kind, "shipped",
                        req.finding.title, req.finding.description or "",
                    )
                except Exception as e:  # pragma: no cover - best-effort
                    logger.debug("finding_memory.remember (shipped) failed: %s", e)

            await _emit("done", status="complete", pr_url=pr_url,
                        files=[c.path for c in committed])
            return ActuateResponse(
                status="complete",
                pr_url=pr_url,
                branch_name=branch_name,
                files_changed=committed,
                skipped=coder_out.skipped,
                summary=coder_out.summary,
            )
        finally:
            # Always release path claims — whether we succeeded, returned a
            # rejection mid-try, or raised. A leaked claim would block the
            # path until its 10-min TTL lapses.
            for p in claimed_paths:
                try:
                    ir.release_path(repo_full, req.branch, p, claimer)
                except Exception as e:
                    logger.debug("release_path failed for %s: %s", p, e)
