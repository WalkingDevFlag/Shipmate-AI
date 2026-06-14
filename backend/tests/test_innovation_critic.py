"""Tests for innovation_critic — the blue-sky verify posture.

The central proof (the reason this module exists): a speculative-but-grounded
idea must SURVIVE the innovation critic (feasibility/coherence) while the
conservative opportunity critic (suppress-novelty) DROPS the same idea. If both
postures behaved the same, innovation mode would be pointless.

The deterministic floor (ground + already-built) is shared with
opportunity_critic and tested there; here we focus on the LLM-posture contrast
and the fail-open contract.
"""
import os
import tempfile

import pytest

_TMP_DB = os.path.join(tempfile.gettempdir(), "shipmate_innovcritic_test.db")
os.environ["SHIPMATE_INFLIGHT_DB"] = _TMP_DB

from app.schemas.agent_schemas import Opportunity  # noqa: E402
from app.services import innovation_critic as ic  # noqa: E402
from app.services import opportunity_critic as oc  # noqa: E402


def _opp(title, *, category="improvement", evidence=None, description="d"):
    return Opportunity(
        id="OPP-000", title=title, category=category,
        description=description, impact="big", effort="M", estimated_days=5,
        target_files=["backend/app/llm_service.py"],
        evidence=evidence or ["backend/app/llm_service.py:_score — keyword only"],
        rationale="anchored to a real seam", source="discovery",
    )


class _FakeProvider:
    """Stub provider whose verdict for a given title is scripted. `mode`
    selects which schema/field the report uses so the SAME fake can drive both
    critics."""

    def __init__(self, refute_titles, mode):
        self.refute = {t.strip().lower() for t in refute_titles}
        self.mode = mode  # "opportunity" | "innovation"
        self.calls = 0

    def invoke_structured_sync(self, *, system_prompt, user_prompt, schema_class, deployment_hint="smart"):
        self.calls += 1
        # Build one verdict per title found in the listing, echoing titles.
        verdicts = []
        for line in user_prompt.splitlines():
            # listing lines look like "1. [cat] Title: desc ..."
            if ". [" not in line or "]" not in line:
                continue
            after = line.split("] ", 1)[1] if "] " in line else ""
            title = after.split(":", 1)[0].strip()
            if not title:
                continue
            refuted = title.lower() in self.refute
            if self.mode == "opportunity":
                verdicts.append({"title": title, "worth_doing": not refuted,
                                 "reason": "scripted"})
            else:
                verdicts.append({"title": title, "feasible_and_real": not refuted,
                                 "reason": "scripted"})
        return schema_class(verdicts=verdicts)


# ── The central proof: postures diverge on the SAME speculative idea ─────────

class TestPostureContrast:
    def _spec_idea(self):
        return _opp(
            "Add a self-improving verify-refine loop to RepoLens",
            description="(exploratory) wrap RepoLens in a verify→refine loop that "
                        "re-scores its own output before emitting — speculative.",
        )

    def test_conservative_critic_drops_speculative_idea(self):
        # The conservative critic is told to suppress speculative/future work.
        idea = self._spec_idea()
        provider = _FakeProvider(refute_titles=[idea.title], mode="opportunity")
        kept = oc.verify_opportunities([idea], "CODE BLOB", provider)
        assert kept == []  # dropped as "not worth doing" (speculative)

    def test_innovation_critic_keeps_the_same_speculative_idea(self):
        # The innovation critic keeps it — feasible + anchored, speculation OK.
        idea = self._spec_idea()
        provider = _FakeProvider(refute_titles=[], mode="innovation")
        kept = ic.verify_innovations([idea], "CODE BLOB", provider)
        assert len(kept) == 1
        assert kept[0].title == idea.title
        assert kept[0].worth_doing is True

    def test_innovation_critic_still_drops_incoherent_idea(self):
        # It is not a rubber stamp: an incoherent/un-prototypable idea is dropped.
        idea = _opp("Rewrite the entire stack in a new language this sprint")
        provider = _FakeProvider(refute_titles=[idea.title], mode="innovation")
        kept = ic.verify_innovations([idea], "CODE BLOB", provider)
        assert kept == []


# ── Fail-open contract ───────────────────────────────────────────────────────

class TestFailOpen:
    def test_no_provider_returns_input_unchanged(self):
        ideas = [_opp("x"), _opp("y")]
        assert ic.verify_innovations(ideas, "CODE", None) == ideas

    def test_empty_code_blob_returns_input(self):
        ideas = [_opp("x")]
        assert ic.verify_innovations(ideas, "", _FakeProvider([], "innovation")) == ideas

    def test_provider_exception_keeps_all(self):
        class _Boom:
            def invoke_structured_sync(self, **kw):
                raise RuntimeError("bedrock down")
        ideas = [_opp("x"), _opp("y")]
        assert ic.verify_innovations(ideas, "CODE", _Boom()) == ideas

    def test_disabled_via_env(self, monkeypatch):
        monkeypatch.setattr(ic, "_VERIFY_ENABLED", False)
        ideas = [_opp("x")]
        # Even a provider that would refute is bypassed when disabled.
        assert ic.verify_innovations(ideas, "CODE", _FakeProvider(["x"], "innovation")) == ideas


# ── Re-exported deterministic floor still works through innovation_critic ────

class TestSharedFloor:
    def test_grounding_reexport(self):
        tree = ["backend/app/llm_service.py"]
        o = _opp("real", evidence=["backend/app/llm_service.py:foo"])
        ic.ground_opportunities([o], tree, {})
        assert o.grounded is True
        # Fully fake: both evidence AND target_files must miss the tree, since
        # ground_opportunities grounds on either.
        ungrounded = Opportunity(
            id="OPP-001", title="fake", category="improvement", description="d",
            impact="big", effort="M", estimated_days=5,
            target_files=["backend/app/nope.py"],
            evidence=["backend/app/nope.py:foo"],
            rationale="r", source="discovery",
        )
        ic.ground_opportunities([ungrounded], tree, {})
        assert ungrounded.grounded is False
