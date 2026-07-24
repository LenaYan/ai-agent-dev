from cn_curriculum_graph.models import Provenance, Standard
from cn_curriculum_graph.validators.base import Severity
from cn_curriculum_graph.validators.coverage import (
    check_provenance_present,
    check_standards_coverage,
)
from conftest import graph, topic

ALIGNED = [Standard(curriculum="cn-moe-math-2022", code="3.1.2")]


def test_standards_coverage_below_threshold_is_an_error():
    # 3 个节点只有 1 个对齐了课标 = 33%，低于默认阈值 90%
    g = graph(
        topics=[
            topic("A", standards=ALIGNED),
            topic("B"),
            topic("C"),
        ]
    )

    findings = check_standards_coverage(g)

    assert [f.code for f in findings] == ["LOW_STANDARDS_COVERAGE"]
    assert findings[0].severity is Severity.ERROR
    assert findings[0].context["ratio"] == 1 / 3
    assert set(findings[0].context["unaligned_ids"]) == {"B", "C"}


def test_standards_coverage_meeting_threshold_passes():
    g = graph(topics=[topic("A", standards=ALIGNED), topic("B", standards=ALIGNED)])

    assert check_standards_coverage(g) == []


def test_threshold_is_configurable():
    g = graph(topics=[topic("A", standards=ALIGNED), topic("B")])

    assert check_standards_coverage(g, min_ratio=0.5) == []
    assert check_standards_coverage(g, min_ratio=0.6) != []


def test_empty_graph_does_not_divide_by_zero():
    assert check_standards_coverage(graph()) == []


def test_topic_without_provenance_is_reported():
    # 没有 provenance 就无法判断这条数据可不可信——Marble 最大的信任缺口
    g = graph(topics=[topic("A", provenance=Provenance(method="hand-authored")), topic("B")])

    findings = check_provenance_present(g)

    assert [f.code for f in findings] == ["MISSING_PROVENANCE"]
    assert findings[0].context["topic_id"] == "B"
