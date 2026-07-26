"""名实一致校验：三级判定 → 两级严重性。

判定分三档而不是 true/false，因为真实数据（Marble 抽样 124 个节点）跑出来
有一整类既不是"讲同一件事"也不是"讲不同知识点"的情形：名称与描述**同属一个
领域，但覆盖范围对不上**（Deep-Sea Survival 的描述扩到了木蛙和水熊虫）。
把它和"乘法阵列 vs 短除法"这种知识点错配一起判 ERROR 会让 CI 过噪，
静默放过又会漏掉真实的命名质量问题 —— 所以拆成 ERROR / WARNING 两级。
"""

import pytest
from pydantic import ValidationError

from cn_curriculum_graph.validators.base import Severity
from cn_curriculum_graph.validators.consistency import (
    Verdict,
    check_name_description_consistency,
)
from conftest import graph, topic


def _judge(judgment: str, reason: str = ""):
    def judge(name: str, description: str) -> Verdict:
        return Verdict(judgment=judgment, reason=reason or f"「{name}」判为 {judgment}")

    return judge


def test_topic_mismatch_is_an_error():
    # Marble 真实案例：节点名叫 Understanding angles，描述却是"用边长相乘求面积"
    g = graph(topics=[topic("A", name="理解角", description="用边长相乘求长方形面积")])

    findings = check_name_description_consistency(
        g, judge=_judge("topic_mismatch", "名说认识角，描述在求面积")
    )

    assert [f.code for f in findings] == ["NAME_DESC_MISMATCH"]
    assert findings[0].severity is Severity.ERROR
    assert findings[0].context["topic_id"] == "A"
    assert "求面积" in findings[0].context["reason"]


def test_scope_mismatch_is_a_warning_not_an_error():
    """名称罩不住描述的范围 —— 是命名质量问题，不是数据错配，不该让 CI 变红。"""
    g = graph(
        topics=[topic("A", name="深海生存", description="深海动物如何抗压，兼及南极鱼、木蛙、水熊虫")]
    )

    findings = check_name_description_consistency(
        g, judge=_judge("scope_mismatch", "描述范围远超『深海』")
    )

    assert [f.code for f in findings] == ["NAME_DESC_SCOPE_MISMATCH"]
    assert findings[0].severity is Severity.WARNING
    assert findings[0].context["topic_id"] == "A"


def test_consistent_topics_produce_no_findings():
    g = graph(topics=[topic("A", name="理解角", description="认识角是由一点引出的两条射线组成")])

    assert check_name_description_consistency(g, judge=_judge("consistent")) == []


def test_judge_is_called_once_per_topic():
    calls: list[tuple[str, str]] = []

    def recording_judge(name: str, description: str) -> Verdict:
        calls.append((name, description))
        return Verdict(judgment="consistent")

    g = graph(topics=[topic("A", name="甲"), topic("B", name="乙")])

    check_name_description_consistency(g, judge=recording_judge)

    assert [name for name, _ in calls] == ["甲", "乙"]


def test_verdict_rejects_unknown_judgment():
    """三档是封闭集合 —— 模型编一个新词出来必须当场炸，而不是被静默当成一致。"""
    with pytest.raises(ValidationError):
        Verdict(judgment="probably_fine")


def test_verdict_exposes_is_consistent_shortcut():
    assert Verdict(judgment="consistent").is_consistent is True
    assert Verdict(judgment="scope_mismatch").is_consistent is False
    assert Verdict(judgment="topic_mismatch").is_consistent is False
