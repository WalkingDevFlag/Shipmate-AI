"""Detection/feedback-loop improvements (the "fix the eyes, not the hand" cycle).

Covers the five phases:
  A. finding-critic blindfold fix — finding-aware code blob exposes deep
     defenses; XFF resolution proof suppresses the recurring false positive.
  B. yield_metrics — acceptance/dismissal rates + gate breakdown from the
     existing stores.
  C. finding_memory.remember_detected — records detections WITHOUT suppressing,
     and never downgrades a terminal (shipped/dismissed) row.
  D. eval-gate flag wiring (the gate itself is exercised by the eval suite).
  E. signature-format guard — all four copies of the kind::title::file scheme
     agree, so drift is caught by CI.
"""
import pytest

from app.services import finding_critic as fc
from app.services import finding_memory as fm
from app.services import yield_metrics


# ── Phase A: critic blindfold fix ────────────────────────────────────────────

class _F:
    def __init__(self, title, description="", file=None, category=""):
        self.title = title
        self.description = description
        self.file = file
        self.category = category


class TestXFFResolutionProof:
    """The exact false positive that recurred on both Bedrock and Azure: the
    rate-limiter-trusts-XFF finding, when the _TRUST_PROXY_HEADERS gate exists."""

    _MAIN = (
        "class _RateLimiter:\n    ...\n"
        # the gate lives deep — this simulates main.py's real structure
        + ("\n# filler\n" * 50)
        + "_TRUST_PROXY_HEADERS = os.getenv('TRUST_PROXY_HEADERS', 'false')\n"
        + "def _get_client_ip(request):\n    if _TRUST_PROXY_HEADERS: ...\n"
    )

    def test_xff_finding_suppressed_when_gate_present(self):
        findings = [_F("Rate Limiter Trusts Spoofable X-Forwarded-For Header",
                       "attacker can spoof X-Forwarded-For", file="backend/app/main.py")]
        kept = fc.filter_already_resolved(
            findings, ["backend/app/main.py"], {"backend/app/main.py": self._MAIN},
            kind="guardrail",
        )
        assert kept == [], "XFF finding must be dropped when the trust-gate exists"

    def test_xff_finding_kept_when_no_gate(self):
        # No _TRUST_PROXY_HEADERS in the corpus → genuine, keep it.
        findings = [_F("Rate Limiter Trusts Spoofable X-Forwarded-For Header",
                       "spoofable X-Forwarded-For", file="backend/app/main.py")]
        kept = fc.filter_already_resolved(
            findings, ["backend/app/main.py"],
            {"backend/app/main.py": "class _RateLimiter: pass\n"}, kind="guardrail",
        )
        assert len(kept) == 1, "without the gate, the finding is real and must survive"

    @pytest.mark.parametrize("title,desc", [
        ("Host header is spoofable and used for the redirect URL", "attacker spoofs Host"),
        ("Origin header spoofable but trusted for CORS", ""),
        ("Reverse proxy misconfig exposes internal headers", "proxy header leak"),
        ("User-Agent is spoofable and trusted for access decisions", ""),
    ])
    def test_unrelated_spoof_findings_not_over_suppressed(self, title, desc):
        # Review fix: tightened XFF phrases must NOT suppress a DIFFERENT genuine
        # finding that merely contains 'spoofable'/'proxy header' just because the
        # _get_client_ip gate exists in the corpus.
        findings = [_F(title, desc, file="backend/app/main.py")]
        kept = fc.filter_already_resolved(
            findings, ["backend/app/main.py"], {"backend/app/main.py": self._MAIN},
            kind="guardrail",
        )
        assert len(kept) == 1, f"unrelated finding wrongly suppressed: {title!r}"


class TestFindingAwareBlob:
    def test_includes_full_cited_file(self):
        big = "header\n" + ("x = 1\n" * 5000) + "DEEP_DEFENSE = True\n"  # >14k, defense at end
        findings = [_F("issue", file="backend/app/main.py")]
        blob = fc.finding_aware_code_blob(
            findings, {"backend/app/main.py": big}, fallback_blob="(short)",
            max_chars_per_cited=200000,
        )
        assert "DEEP_DEFENSE" in blob, "cited file must be included in FULL"
        assert "(short)" in blob, "fallback blob is preserved for broad context"

    def test_no_citations_returns_fallback(self):
        blob = fc.finding_aware_code_blob([_F("x", file=None)], {}, fallback_blob="FB")
        assert blob == "FB"

    def test_suffix_path_match(self):
        # finding cites 'main.py', key_files keyed by full path
        findings = [_F("x", file="main.py")]
        blob = fc.finding_aware_code_blob(
            findings, {"backend/app/main.py": "MARKER=1"}, fallback_blob="",
        )
        assert "MARKER=1" in blob

    def test_suffix_match_respects_path_boundary(self):
        # Review fix: cited 'main.py' must NOT attach 'domain.py' / 'banana_main.py'
        # content (a bare endswith would). Only a real path-boundary match counts.
        findings = [_F("x", file="main.py")]
        blob = fc.finding_aware_code_blob(
            findings,
            {"backend/app/domain.py": "WRONG_DOMAIN=1", "src/banana_main.py": "WRONG_BANANA=1"},
            fallback_blob="FB",
        )
        assert "WRONG_DOMAIN" not in blob and "WRONG_BANANA" not in blob
        assert blob == "FB"  # nothing matched → pure fallback


# ── Phase C: remember_detected ───────────────────────────────────────────────

class TestRememberDetected:
    def test_detected_does_not_suppress(self):
        repo = "o/r"
        sig = fc.finding_signature("guardrail", "A real ongoing issue", "f.py")
        fm.remember_detected(repo, sig, "guardrail", "A real ongoing issue", "desc")
        # 'detected' is NOT a suppressing state → a rephrase must NOT be suppressed.
        assert fm.is_semantically_suppressed(
            repo, "A real ongoing issue", "desc"
        ) is False, "detected findings must keep surfacing (not suppressed)"

    def test_detected_never_downgrades_shipped(self):
        repo = "o/r2"
        sig = fc.finding_signature("guardrail", "Shipped finding", "f.py")
        fm.remember(repo, sig, "guardrail", "shipped", "Shipped finding", "d")
        # A later detection of the same sig must NOT overwrite 'shipped'.
        fm.remember_detected(repo, sig, "guardrail", "Shipped finding", "d")
        # still suppressed because it remained 'shipped'
        assert fm.is_semantically_suppressed(repo, "Shipped finding", "d") is True

    def test_detection_count_increments(self):
        repo = "o/r3"
        before = fm.detection_count(repo)
        fm.remember_detected(repo, "guardrail::x::f", "guardrail", "X", "")
        assert fm.detection_count(repo) == before + 1


# ── Phase B: yield metrics ───────────────────────────────────────────────────

class TestYieldMetrics:
    def test_empty_is_safe(self):
        y = yield_metrics.compute_yield("nonexistent/repo")
        assert y["detected"] == 0
        assert y["acceptance_rate"] is None
        assert "not enough data" in y["interpretation"].lower()

    def test_acceptance_rate_computed(self, monkeypatch):
        # Stub the journal so the math is deterministic.
        monkeypatch.setattr(
            "app.services.inflight_registry.journal_list",
            lambda repo_full_name=None, state_in=None: [
                {"state": "shipped"}, {"state": "shipped"},
                {"state": "dismissed"}, {"state": "parked"},
            ],
        )
        y = yield_metrics.compute_yield("o/r")
        # 2 shipped of 4 terminal → 0.5
        assert y["journal"]["shipped"] == 2
        assert y["acceptance_rate"] == 0.5
        assert y["dismissal_rate"] == 0.25

    def test_gate_rejections_surface(self, monkeypatch):
        monkeypatch.setattr(
            "app.services.coder_lessons.gate_breakdown",
            lambda repo_full_name=None: {"lint": 3, "scope": 1, "pytest": 2},
        )
        y = yield_metrics.compute_yield("o/r")
        assert y["total_gate_rejections"] == 6
        assert y["gate_rejections"]["lint"] == 3


# ── Phase D: eval-gate wiring ────────────────────────────────────────────────

class TestEvalGateWiring:
    def test_eval_gate_off_by_default(self):
        # Opt-in: the hot actuate path is unchanged until SHIPMATE_EVAL_GATE=1.
        from app.services.coder_orchestrator import _EVAL_GATE_ENABLED
        assert _EVAL_GATE_ENABLED is False

    def test_define_spec_produces_runnable_spec_for_milestone(self):
        # The gate feeds define_spec(finding) → run_eval_in_worktree. Without a
        # provider it must still yield a non-empty (health-baseline) spec.
        from app.services import define_validation
        from app.schemas.api_schemas import FindingPayload
        f = FindingPayload(kind="milestone", id="M1", title="Add a thing", description="d")
        spec = define_validation.define_spec(f, provider=None)
        assert not spec.is_empty()

    def test_eval_rejection_lesson_not_miscategorized_as_pytest(self):
        # Review fix: an EvalOps rejection whose text contains 'test' must
        # distill to the eval lesson, not pytest-regression.
        from app.services import coder_lessons
        key, lesson = coder_lessons.distill(
            "eval", "EvalOps spec failed: the /widgets scenario test did not pass"
        )
        assert key == "eval-acceptance"
        assert "ACCEPTANCE" in lesson


# ── Phase E: signature-format guard (drift catcher) ──────────────────────────

class TestSignatureParity:
    def test_all_signature_impls_agree(self):
        from app.services.coder_orchestrator import _finding_signature as co_sig
        from app.api.routes.findings import finding_signature as routes_sig
        from app.schemas.api_schemas import FindingPayload

        kind, title, file = "guardrail", "Some Finding Title", "backend/app/main.py"
        payload = FindingPayload(kind=kind, id="X", title=title, description="d", file=file)
        canonical = fc.finding_signature(kind, title, file)
        assert co_sig(payload) == canonical
        assert routes_sig(kind, title, file) == canonical

    def test_coder_loop_signature_format_matches(self):
        # coder_loop is heavy to import; assert its FORMAT string instead.
        import re
        src = open("scripts/coder_loop.py").read()
        m = re.search(r"def _finding_signature.*?return (f\".*?\")", src, re.DOTALL)
        assert m, "coder_loop._finding_signature not found"
        fmt = m.group(1)
        # must produce kind::<lowered,capped title>::<lowered file>
        assert "::" in fmt and "lower()" in fmt and "[:80]" in fmt
