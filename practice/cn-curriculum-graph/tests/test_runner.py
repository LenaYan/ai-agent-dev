from cn_curriculum_graph.models import Provenance, Standard
from cn_curriculum_graph.runner import has_errors, run_all
from cn_curriculum_graph.validators.base import Finding, Severity
from cn_curriculum_graph.validators.consistency import Verdict
from conftest import dep, graph, topic


def clean_topic(tid: str, **kw):
    return topic(
        tid,
        standards=[Standard(curriculum="cn-moe-math-2022", code="3.1.2")],
        provenance=Provenance(method="hand-authored", review_status="self_reviewed"),
        **kw,
    )


def judge_ok(name: str, description: str) -> Verdict:
    return Verdict(consistent=True, reason="")


def test_clean_graph_yields_no_findings():
    g = graph(
        topics=[clean_topic("A", grade_start=4), clean_topic("B", grade_start=3)],
        dependencies=[dep("A", "B")],
    )

    assert run_all(g, judge=judge_ok) == []


def test_collects_findings_from_every_validator_layer():
    # 同时埋三个问题：环、缺 provenance、课标未对齐
    g = graph(
        topics=[topic("A"), topic("B")],
        dependencies=[dep("A", "B"), dep("B", "A")],
    )

    codes = {f.code for f in run_all(g, judge=judge_ok)}

    assert {"CYCLE", "MISSING_PROVENANCE", "LOW_STANDARDS_COVERAGE"} <= codes


def test_skipping_consistency_check_is_reported_not_silent():
    # 不传 judge 时跳过语义校验，但必须在结果里说明跳过了什么——
    # 静默略过会让 CI 通过看起来像"全查过了"
    g = graph(
        topics=[clean_topic("A", grade_start=4), clean_topic("B", grade_start=3)],
        dependencies=[dep("A", "B")],
    )

    findings = run_all(g, judge=None)

    assert [f.code for f in findings] == ["CONSISTENCY_SKIPPED"]
    assert findings[0].severity is Severity.WARNING


def test_has_errors_is_false_when_only_warnings():
    findings = [Finding(code="X", severity=Severity.WARNING, message="")]

    assert has_errors(findings) is False


def test_has_errors_is_true_when_any_error_present():
    findings = [
        Finding(code="X", severity=Severity.WARNING, message=""),
        Finding(code="Y", severity=Severity.ERROR, message=""),
    ]

    assert has_errors(findings) is True
