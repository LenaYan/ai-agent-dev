"""内容层校验：name 与 description 是否讲的是同一件事。

这是唯一一个需要语义判断、无法靠纯规则完成的校验，因此把判定器
（judge）做成依赖注入：CI 里接真 LLM，测试里接确定性假判定器。
"""

from typing import Protocol

from pydantic import BaseModel, ConfigDict

from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.validators.base import Finding, Severity


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    consistent: bool
    reason: str = ""


class Judge(Protocol):
    def __call__(self, name: str, description: str) -> Verdict: ...


def check_name_description_consistency(
    graph: CurriculumGraph, judge: Judge
) -> list[Finding]:
    """节点名必须和描述讲同一件事。

    Marble 至少有 4 个已确认的反例（如 mt_GzcJEVkNRn 名为 Understanding angles、
    描述却是用边长相乘求面积）。这类错误纯规则查不出来，但会让所有按 name
    检索的下游应用拿到错的东西。
    """
    findings: list[Finding] = []

    for t in graph.topics:
        verdict = judge(name=t.name, description=t.description)
        if verdict.consistent:
            continue
        findings.append(
            Finding(
                code="NAME_DESC_MISMATCH",
                severity=Severity.ERROR,
                message=f"节点 {t.id} 的名称与描述不一致：{verdict.reason}",
                context={
                    "topic_id": t.id,
                    "name": t.name,
                    "reason": verdict.reason,
                },
            )
        )

    return findings
