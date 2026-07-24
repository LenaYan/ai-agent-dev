"""覆盖率层校验：课标对齐率、来源完整性。"""

from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.validators.base import Finding, Severity

DEFAULT_MIN_STANDARDS_RATIO = 0.9


def check_standards_coverage(
    graph: CurriculumGraph, min_ratio: float = DEFAULT_MIN_STANDARDS_RATIO
) -> list[Finding]:
    """课标对齐率必须达标。

    存在的理由：Marble 对外宣称"对齐国家课标"，实际 1590 个节点里
    772 个（48.6%）的 standards 字段是空的。宣称与事实的差距没有任何
    机制去卡，就一定会发生。
    """
    if not graph.topics:
        return []

    unaligned = [t.id for t in graph.topics if not t.standards]
    total = len(graph.topics)
    ratio = (total - len(unaligned)) / total
    if ratio >= min_ratio:
        return []

    return [
        Finding(
            code="LOW_STANDARDS_COVERAGE",
            severity=Severity.ERROR,
            message=(
                f"课标对齐率 {ratio:.1%} 低于阈值 {min_ratio:.1%}"
                f"（{len(unaligned)}/{len(graph.topics)} 个节点未对齐）"
            ),
            context={
                "ratio": ratio,
                "min_ratio": min_ratio,
                "unaligned_ids": unaligned,
            },
        )
    ]


def check_provenance_present(graph: CurriculumGraph) -> list[Finding]:
    """每个节点都要能回答"你从哪来、谁审过"。"""
    return [
        Finding(
            code="MISSING_PROVENANCE",
            severity=Severity.ERROR,
            message=f"节点 {t.id}（{t.name}）缺少 provenance，无法判断可信度",
            context={"topic_id": t.id},
        )
        for t in graph.topics
        if t.provenance is None
    ]
