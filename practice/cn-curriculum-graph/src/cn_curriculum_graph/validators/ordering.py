"""顺序层校验：年级单调性、螺旋边递进性。

这两条是 Marble 数据里暴露出问题的地方（3221 条边中 56 条年级倒挂，
其中 8 条是 hard 边），所以在本项目里从一开始就进 CI。
"""

from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.validators.base import Finding, Severity


def check_grade_monotonic(graph: CurriculumGraph) -> list[Finding]:
    """先修节点的起始年级不得晚于后继节点。

    hard 边倒挂是硬错误：真的先修关系不可能"先学后面的"。
    soft 边降级为警告：互相支撑的概念在同一学段来回引用是合理的。
    """
    by_id = graph.by_id
    findings: list[Finding] = []

    for d in graph.dependencies:
        succ, pre = by_id.get(d.topic_id), by_id.get(d.prerequisite_id)
        if succ is None or pre is None:
            continue  # 悬挂引用由 check_no_dangling_refs 负责报告
        if pre.grade_start <= succ.grade_start:
            continue

        findings.append(
            Finding(
                code="GRADE_INVERSION",
                severity=Severity.ERROR if d.strength == "hard" else Severity.WARNING,
                message=(
                    f"{d.strength} 先修边年级倒挂："
                    f"前置「{pre.name}」({pre.grade_start}年级) 晚于 "
                    f"后继「{succ.name}」({succ.grade_start}年级)"
                ),
                context={
                    "topic_id": d.topic_id,
                    "prerequisite_id": d.prerequisite_id,
                    "strength": d.strength,
                },
            )
        )

    return findings


def check_revisit_advances_grade(graph: CurriculumGraph) -> list[Finding]:
    """螺旋边必须指向更高年级——同级或倒退说明边接反了。"""
    by_id = graph.by_id
    findings: list[Finding] = []

    for r in graph.revisits:
        earlier, later = by_id.get(r.earlier_id), by_id.get(r.later_id)
        if earlier is None or later is None:
            continue
        if later.grade_start > earlier.grade_start:
            continue

        findings.append(
            Finding(
                code="REVISIT_NOT_ADVANCING",
                severity=Severity.ERROR,
                message=(
                    f"螺旋边未递进：「{earlier.name}」({earlier.grade_start}年级) → "
                    f"「{later.name}」({later.grade_start}年级)"
                ),
                context={"earlier_id": r.earlier_id, "later_id": r.later_id},
            )
        )

    return findings
