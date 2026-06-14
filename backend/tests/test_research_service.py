"""Tests for ResearchService — the codebase deep-research coordinator.

Proves: (1) the reference graph is always computed (deterministic grounding),
(2) the LLM findings are coerced + clamped, (3) fail-open yields a graph-only
report rather than an error.
"""
import pytest

from app.schemas.agent_schemas import ResearchReport
from app.services import llm_service as ls
from app.services.research_service import ResearchService

CTX = {
    "repo_info": {"owner": {"login": "o"}, "name": "r"},
    "branch": "main",
    "key_files": {
        "backend/app/a.py": "from app.b import helper\ndef run():\n    return helper()\n",
        "backend/app/b.py": "from app.a import run\ndef helper():\n    return run()\ndef dead():\n    return 2\n",
    },
    "file_tree": ["backend/app/a.py", "backend/app/b.py"],
}


class _Finding:
    def __init__(self, **kw):
        self.__dict__.update(kw)


class _Discovery:
    def __init__(self, answer, findings):
        self.answer = answer
        self.findings = findings


@pytest.fixture
def _restore_llm():
    orig_research = ls.LLMService.research_codebase
    orig_avail = ls.LLMService.is_available
    yield
    ls.LLMService.research_codebase = orig_research
    ls.LLMService.is_available = orig_avail


class TestGroundingAlwaysComputed:
    def test_graph_summary_present_even_without_llm(self, _restore_llm):
        ls.LLMService.research_codebase = classmethod(lambda cls, *a, **k: None)
        rep = ResearchService.research(CTX, question="cycles?")
        assert isinstance(rep, ResearchReport)
        assert rep.ai_enhanced is False           # LLM unavailable
        assert rep.graph_summary["module_count"] == 2
        assert rep.graph_summary["cycles"]         # a<->b still detected
        assert rep.findings == []                  # no LLM → no findings, but no error


class TestLLMFindingsCoerced:
    def test_findings_mapped_and_clamped(self, _restore_llm):
        findings = [
            _Finding(title="Break a<->b cycle", kind="coupling", severity="high",
                     detail="circular import", evidence=["backend/app/a.py", "backend/app/b.py", "x", "y"],
                     suggested_action="extract helper", graph_signal="cycle: a.py<->b.py"),
            _Finding(title="weird kind", kind="not_a_kind", severity="banana",
                     detail="d", evidence=[], suggested_action="", graph_signal=""),
        ]
        ls.LLMService.research_codebase = classmethod(
            lambda cls, *a, **k: _Discovery("Yes, there's a cycle.", findings))
        ls.LLMService.is_available = classmethod(lambda cls: True)

        rep = ResearchService.research(CTX, question="cycles?")
        assert rep.ai_enhanced is True
        assert rep.answer.startswith("Yes")
        assert len(rep.findings) == 2
        f0 = rep.findings[0]
        assert f0.kind == "coupling" and f0.severity == "high"
        assert f0.graph_signal == "cycle: a.py<->b.py"
        assert len(f0.evidence) == 3               # clamped to 3
        # invalid enum values fall back to safe defaults
        assert rep.findings[1].kind == "observation"
        assert rep.findings[1].severity == "medium"

    def test_max_findings_respected(self, _restore_llm):
        many = [_Finding(title=f"f{i}", kind="risk", severity="low", detail="d",
                         evidence=["backend/app/a.py"], suggested_action="", graph_signal="")
                for i in range(20)]
        ls.LLMService.research_codebase = classmethod(lambda cls, *a, **k: _Discovery("", many))
        ls.LLMService.is_available = classmethod(lambda cls: True)
        rep = ResearchService.research(CTX, max_findings=5)
        assert len(rep.findings) == 5


class TestOpenAudit:
    def test_no_question_is_open_audit(self, _restore_llm):
        ls.LLMService.research_codebase = classmethod(lambda cls, *a, **k: None)
        rep = ResearchService.research(CTX)   # no question
        assert rep.question == ""
        assert rep.graph_summary["module_count"] == 2


class TestSalvageLeakedFindings:
    """Regression: Opus-on-Bedrock sometimes serializes the `findings` ARRAY
    into the `answer` STRING (an XML param-tag or bare-JSON leak) for the
    two-field research schema. _salvage_research_findings must recover them."""

    def test_xml_param_tag_leak_recovered(self):
        from app.services.llm_service import _salvage_research_findings, _ResearchDiscovery
        leaked = (
            'Prose answer about the repo.\n'
            '<parameter name="findings">'
            '[{"title":"god barrel","kind":"coupling","severity":"high",'
            '"detail":"fan-in 16","evidence":["backend/app/services/__init__.py"],'
            '"suggested_action":"import directly","graph_signal":"fan_in=16"}]'
            '</parameter>'
        )
        out = _salvage_research_findings(_ResearchDiscovery(answer=leaked, findings=[]))
        assert len(out.findings) == 1
        assert out.findings[0].graph_signal == "fan_in=16"
        assert "parameter" not in out.answer and out.answer == "Prose answer about the repo."

    def test_bare_json_leak_recovered(self):
        from app.services.llm_service import _salvage_research_findings, _ResearchDiscovery
        leaked = 'Answer text. "findings": [{"title":"x","kind":"risk","severity":"low","detail":"d"}]'
        out = _salvage_research_findings(_ResearchDiscovery(answer=leaked, findings=[]))
        assert len(out.findings) == 1 and out.findings[0].kind == "risk"

    def test_noop_when_findings_present(self):
        from app.services.llm_service import _salvage_research_findings, _ResearchDiscovery, _ResearchFinding
        disc = _ResearchDiscovery(answer="clean", findings=[_ResearchFinding(title="t", kind="risk", severity="low", detail="d")])
        assert _salvage_research_findings(disc).answer == "clean"

    def test_noop_when_no_leak(self):
        from app.services.llm_service import _salvage_research_findings, _ResearchDiscovery
        disc = _ResearchDiscovery(answer="just prose, no array here", findings=[])
        out = _salvage_research_findings(disc)
        assert out.findings == [] and out.answer == "just prose, no array here"

    def test_none_passthrough(self):
        from app.services.llm_service import _salvage_research_findings
        assert _salvage_research_findings(None) is None
