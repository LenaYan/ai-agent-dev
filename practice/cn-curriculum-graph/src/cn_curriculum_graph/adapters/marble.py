"""Marble Skill Taxonomy → 本 schema 的适配器。

**用途仅限于验证校验层**：把规则跑在一份真实的、1590 节点 / 3221 条边的
图上，确认它们在规模下确实能抓到问题。不用于引入 Marble 的内容。

年龄→年级的映射是有损的（中国 6 岁上一年级，故 grade = age − 5），
超出义务教育 1-9 年级范围的一律夹逼。因此适配结果**不可**当作
中国课标数据使用。

Marble 数据许可：ODbL 1.0（数据库）+ CC BY-SA 4.0（内容）。
"""

from typing import Any

from cn_curriculum_graph.models import (
    GRADE_MAX,
    GRADE_MIN,
    CurriculumGraph,
    Dependency,
    Standard,
    Topic,
)

EVIDENCE_PLACEHOLDER = "（源数据缺失）"
SCHOOL_ENTRY_AGE = 6


def _to_grade(age: int) -> int:
    """6 岁 → 一年级；超出义务教育范围的夹逼到 [1, 9]。"""
    return max(GRADE_MIN, min(GRADE_MAX, age - SCHOOL_ENTRY_AGE + 1))


def _to_standards(raw: list[str]) -> list[Standard]:
    """Marble 的 standards 形如 "ccss-math:4.NF.C.6"，冒号左边是课标标识。"""
    out: list[Standard] = []
    for item in raw:
        curriculum, _, code = item.partition(":")
        if code:
            out.append(Standard(curriculum=curriculum, code=code))
    return out


def from_marble(topics_doc: dict[str, Any], deps_doc: dict[str, Any]) -> CurriculumGraph:
    topics = [
        Topic(
            id=raw["id"],
            name=raw["name"],
            description=raw["description"],
            type=raw["type"].lower(),
            subject=raw["subject"],
            domain=raw["domain"],
            grade_start=_to_grade(raw["ageRangeStart"]),
            grade_end=_to_grade(raw["ageRangeEnd"]),
            evidence=raw.get("evidence") or [EVIDENCE_PLACEHOLDER],
            assessment_prompt=raw["assessmentPrompt"],
            standards=_to_standards(raw.get("standards") or []),
        )
        for raw in topics_doc.get("topics", [])
    ]

    dependencies = [
        Dependency(
            topic_id=raw["topicId"],
            prerequisite_id=raw["prerequisiteId"],
            strength=raw["strength"],
            reason=raw["reason"],
        )
        for raw in deps_doc.get("dependencies", [])
        if raw["topicId"] != raw["prerequisiteId"]  # 本 schema 禁止自环
    ]

    return CurriculumGraph(topics=topics, dependencies=dependencies)
