from cn_curriculum_graph.validators.base import Severity
from cn_curriculum_graph.validators.consistency import (
    Verdict,
    check_name_description_consistency,
)
from conftest import graph, topic


def judge_all_consistent(name: str, description: str) -> Verdict:
    return Verdict(consistent=True, reason="")


def judge_all_inconsistent(name: str, description: str) -> Verdict:
    return Verdict(consistent=False, reason=f"「{name}」与描述讲的不是一回事")


def test_reports_topic_whose_name_contradicts_its_description():
    # Marble 的真实案例：节点名叫 Understanding angles，描述却是"用边长相乘求面积"
    g = graph(topics=[topic("A", name="理解角", description="用边长相乘求长方形面积")])

    findings = check_name_description_consistency(g, judge=judge_all_inconsistent)

    assert [f.code for f in findings] == ["NAME_DESC_MISMATCH"]
    assert findings[0].severity is Severity.ERROR
    assert findings[0].context["topic_id"] == "A"
    assert "不是一回事" in findings[0].context["reason"]


def test_consistent_topics_produce_no_findings():
    g = graph(topics=[topic("A", name="理解角", description="认识角是由一点引出的两条射线组成")])

    assert check_name_description_consistency(g, judge=judge_all_consistent) == []


def test_judge_is_called_once_per_topic():
    calls: list[tuple[str, str]] = []

    def recording_judge(name: str, description: str) -> Verdict:
        calls.append((name, description))
        return Verdict(consistent=True, reason="")

    g = graph(topics=[topic("A", name="甲"), topic("B", name="乙")])

    check_name_description_consistency(g, judge=recording_judge)

    assert [name for name, _ in calls] == ["甲", "乙"]
