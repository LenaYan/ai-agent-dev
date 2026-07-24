"""把各层校验器串成一次完整的 CI 运行。"""

from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.validators.base import Finding, Severity
from cn_curriculum_graph.validators.consistency import (
    Judge,
    check_name_description_consistency,
)
from cn_curriculum_graph.validators.coverage import (
    check_provenance_present,
    check_standards_coverage,
)
from cn_curriculum_graph.validators.ordering import (
    check_grade_monotonic,
    check_revisit_advances_grade,
)
from cn_curriculum_graph.validators.structure import (
    check_no_cycles,
    check_no_dangling_refs,
    check_no_isolated_topics,
)

RULE_FREE_CHECKS = (
    check_no_cycles,
    check_no_dangling_refs,
    check_no_isolated_topics,
    check_grade_monotonic,
    check_revisit_advances_grade,
    check_standards_coverage,
    check_provenance_present,
)


def run_all(graph: CurriculumGraph, judge: Judge | None = None) -> list[Finding]:
    """跑全部校验。

    judge 为 None 时跳过语义一致性校验，但会显式产出一条 WARNING ——
    静默略过会让"CI 绿了"看起来像"全都查过了"，这正是 Marble 让人
    误判其数据质量的方式。
    """
    findings: list[Finding] = []
    for check in RULE_FREE_CHECKS:
        findings.extend(check(graph))

    if judge is None:
        findings.append(
            Finding(
                code="CONSISTENCY_SKIPPED",
                severity=Severity.WARNING,
                message="未提供 judge，已跳过 name/description 语义一致性校验",
                context={"skipped_check": "check_name_description_consistency"},
            )
        )
    else:
        findings.extend(check_name_description_consistency(graph, judge=judge))

    return findings


def has_errors(findings: list[Finding]) -> bool:
    return any(f.severity is Severity.ERROR for f in findings)
