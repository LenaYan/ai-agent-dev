from cn_curriculum_graph.validators.structure import (
    check_no_cycles,
    check_no_dangling_refs,
    check_no_isolated_topics,
)
from conftest import dep, graph, topic


def test_reports_cycle_in_prerequisite_edges():
    # A 依赖 B，B 依赖 C，C 又依赖 A —— 学不动的死循环
    g = graph(
        topics=[topic("A"), topic("B"), topic("C")],
        dependencies=[dep("A", "B"), dep("B", "C"), dep("C", "A")],
    )

    findings = check_no_cycles(g)

    assert [f.code for f in findings] == ["CYCLE"]
    assert set(findings[0].context["cycle"]) == {"A", "B", "C"}


def test_accepts_acyclic_chain():
    # 菱形依赖：A←B、A←C、B←D、C←D，无环
    g = graph(
        topics=[topic("A"), topic("B"), topic("C"), topic("D")],
        dependencies=[dep("A", "B"), dep("A", "C"), dep("B", "D"), dep("C", "D")],
    )

    assert check_no_cycles(g) == []


def test_reports_dependency_pointing_at_unknown_prerequisite():
    g = graph(topics=[topic("A")], dependencies=[dep("A", "GHOST")])

    findings = check_no_dangling_refs(g)

    assert [f.code for f in findings] == ["DANGLING_REF"]
    assert findings[0].context["missing_id"] == "GHOST"


def test_reports_revisit_edge_pointing_at_unknown_topic():
    from cn_curriculum_graph.models import Revisit

    g = graph(
        topics=[topic("A")],
        revisits=[Revisit(earlier_id="A", later_id="GHOST", note="第二轮深化")],
    )

    findings = check_no_dangling_refs(g)

    assert [f.code for f in findings] == ["DANGLING_REF"]
    assert findings[0].context["missing_id"] == "GHOST"


def test_reports_topic_with_no_edges_at_all():
    g = graph(
        topics=[topic("A"), topic("B"), topic("LONELY")],
        dependencies=[dep("A", "B")],
    )

    findings = check_no_isolated_topics(g)

    assert [f.code for f in findings] == ["ISOLATED_TOPIC"]
    assert findings[0].context["topic_id"] == "LONELY"


def test_topic_connected_only_by_revisit_edge_is_not_isolated():
    from cn_curriculum_graph.models import Revisit

    g = graph(
        topics=[topic("A", grade_start=3), topic("B", grade_start=5)],
        revisits=[Revisit(earlier_id="A", later_id="B", note="第二轮深化")],
    )

    assert check_no_isolated_topics(g) == []
