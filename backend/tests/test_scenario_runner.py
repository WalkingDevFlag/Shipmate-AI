"""scenario_runner — the pure assertion engine for EvalOps (Phase 5).

Pure functions, so these are fast + deterministic. Pin: JSONPath-lite
resolution (dicts, list indices, missing), each assertion operator, scenario
status+body evaluation, metric expectations, log-clean, and report assembly's
AND semantics.
"""
from app.services.eval_schemas import (
    Assertion, LogExpectation, MetricExpectation, Scenario, ValidationSpec,
)
from app.services import scenario_runner as sr


class TestResolvePath:
    def test_nested_dict(self):
        d = {"agents": {"repo_lens": {"file_count": 42}}}
        assert sr.resolve_path(d, "agents.repo_lens.file_count") == 42

    def test_list_index(self):
        d = {"items": [{"id": "a"}, {"id": "b"}]}
        assert sr.resolve_path(d, "items.1.id") == "b"

    def test_negative_index(self):
        assert sr.resolve_path({"xs": [1, 2, 3]}, "xs.-1") == 3

    def test_missing_returns_sentinel(self):
        assert sr.resolve_path({"a": 1}, "b") is sr._MISSING
        assert sr.resolve_path({"a": 1}, "a.b") is sr._MISSING
        assert sr.resolve_path({"xs": [1]}, "xs.5") is sr._MISSING

    def test_empty_path_returns_root(self):
        assert sr.resolve_path({"a": 1}, "") == {"a": 1}


class TestAssertions:
    def test_equals_pass_and_fail(self):
        assert sr.eval_assertion({"x": 1}, Assertion(path="x", equals=1)) is None
        assert sr.eval_assertion({"x": 1}, Assertion(path="x", equals=2)) is not None

    def test_contains(self):
        assert sr.eval_assertion({"s": "ShipMate AI"}, Assertion(path="s", contains="Ship")) is None
        assert sr.eval_assertion({"s": "x"}, Assertion(path="s", contains="Ship")) is not None

    def test_gte_lte(self):
        assert sr.eval_assertion({"n": 80}, Assertion(path="n", gte=70)) is None
        assert sr.eval_assertion({"n": 60}, Assertion(path="n", gte=70)) is not None
        assert sr.eval_assertion({"n": 60}, Assertion(path="n", lte=70)) is None

    def test_gte_on_nonnumeric_fails(self):
        assert sr.eval_assertion({"n": "x"}, Assertion(path="n", gte=1)) is not None

    def test_bool_is_not_numeric_for_gte(self):
        # True == 1 in Python; guard against a bool sneaking past a numeric gte.
        assert sr.eval_assertion({"b": True}, Assertion(path="b", gte=1)) is not None

    def test_exists_default(self):
        assert sr.eval_assertion({"x": None}, Assertion(path="x")) is None   # present, even if None
        assert sr.eval_assertion({}, Assertion(path="x")) is not None        # absent


class TestScenarioEval:
    def test_status_mismatch_fails(self):
        scn = Scenario(name="s", url="/x", expect_status=200)
        res = sr.eval_scenario(scn, 500, {})
        assert res.passed is False
        assert any("status 500" in f for f in res.failures)

    def test_body_assertions_only_checked_on_status_match(self):
        scn = Scenario(name="s", url="/x", expect_status=200,
                       assertions=[Assertion(path="ok", equals=True)])
        # Wrong status → don't pile on body failures (one clear reason).
        res = sr.eval_scenario(scn, 404, {"error": "nope"})
        assert res.passed is False
        assert len(res.failures) == 1

    def test_all_pass(self):
        scn = Scenario(name="s", url="/x", expect_status=200,
                       assertions=[Assertion(path="v", gte=1)])
        res = sr.eval_scenario(scn, 200, {"v": 5})
        assert res.passed is True


class TestMetricsAndLogs:
    def test_metric_must_fire(self):
        exp = [MetricExpectation(name="report_persisted", must_fire=True)]
        assert sr.eval_metrics(exp, {"report_persisted"})[0].satisfied is True
        assert sr.eval_metrics(exp, set())[0].satisfied is False

    def test_metric_must_not_fire(self):
        exp = [MetricExpectation(name="error_counter", must_fire=False)]
        assert sr.eval_metrics(exp, set())[0].satisfied is True
        assert sr.eval_metrics(exp, {"error_counter"})[0].satisfied is False

    def test_logs_clean_and_dirty(self):
        clean, viol = sr.eval_logs("INFO ok\nINFO done", LogExpectation())
        assert clean is True and viol == []
        clean, viol = sr.eval_logs("INFO ok\nERROR boom\n", LogExpectation())
        assert clean is False and viol


class TestAssembleReport:
    def test_all_green_passes(self):
        spec = ValidationSpec(name="s")
        rep = sr.assemble_report(
            spec, [sr.ScenarioResult(name="a", passed=True)],
            [], True, [],
        )
        assert rep.passed is True

    def test_any_failure_fails(self):
        spec = ValidationSpec(name="s")
        rep = sr.assemble_report(
            spec, [sr.ScenarioResult(name="a", passed=False, failures=["x"])],
            [], True, [],
        )
        assert rep.passed is False

    def test_error_overrides_to_fail(self):
        spec = ValidationSpec(name="s")
        rep = sr.assemble_report(spec, [], [], True, [], error="boot failed")
        assert rep.passed is False
        assert rep.ran is False
