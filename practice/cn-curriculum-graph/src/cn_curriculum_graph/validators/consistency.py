"""内容层校验：name 与 description 是否讲的是同一件事。

这是唯一一个需要语义判断、无法靠纯规则完成的校验，因此把判定器
（judge）做成依赖注入：CI 里接真 LLM，测试里接确定性假判定器。
"""

from typing import Literal, Protocol

from pydantic import BaseModel, ConfigDict

from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.validators.base import Finding, Severity

# 三档而非 true/false。真实数据上有一整类"同一领域、覆盖范围对不上"的情形
# （Deep-Sea Survival 的描述扩到木蛙和水熊虫），既不是讲同一件事，也不是
# 知识点错配。用 Literal 而非 Enum：pydantic 会把它内联成 {"enum": [...]}，
# 不生成 $defs 引用 —— 各家结构化输出/工具 schema 对 $ref 的支持参差不齐。
Judgment = Literal["consistent", "scope_mismatch", "topic_mismatch"]


class Verdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    judgment: Judgment
    reason: str = ""

    @property
    def is_consistent(self) -> bool:
        return self.judgment == "consistent"


class Judge(Protocol):
    def __call__(self, name: str, description: str) -> Verdict: ...


# 判定 → (finding code, 严重级)。scope_mismatch 是命名质量问题而非数据错配，
# 判 WARNING：判 ERROR 会让 CI 被一堆"名字起窄了"的节点刷红，静默放过又漏掉
# 真实的命名问题。
_OUTCOME: dict[str, tuple[str, Severity]] = {
    "topic_mismatch": ("NAME_DESC_MISMATCH", Severity.ERROR),
    "scope_mismatch": ("NAME_DESC_SCOPE_MISMATCH", Severity.WARNING),
}


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
        if verdict.is_consistent:
            continue
        code, severity = _OUTCOME[verdict.judgment]
        problem = "名称与描述讲的不是一回事" if severity is Severity.ERROR else "名称罩不住描述的范围"
        findings.append(
            Finding(
                code=code,
                severity=severity,
                message=f"节点 {t.id} {problem}：{verdict.reason}",
                context={
                    "topic_id": t.id,
                    "name": t.name,
                    "judgment": verdict.judgment,
                    "reason": verdict.reason,
                },
            )
        )

    return findings
