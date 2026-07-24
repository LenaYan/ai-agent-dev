"""测试用的图构造助手。

刻意保持"手写字面量"风格：测试里一眼能看出图长什么样，
不引入 builder DSL——那会把测试变成对 builder 的测试。
"""

from cn_curriculum_graph.models import CurriculumGraph, Dependency, Topic


def topic(tid: str, grade_start: int = 3, grade_end: int | None = None, **kw) -> Topic:
    """构造一个字段齐全的最小 Topic，只有关心的字段需要显式传。"""
    grade_end = grade_start if grade_end is None else grade_end
    defaults = dict(
        id=tid,
        name=f"节点{tid}",
        description=f"节点{tid}的描述",
        type="conceptual",
        subject="数学",
        domain="数与代数",
        grade_start=grade_start,
        grade_end=grade_end,
        evidence=[f"{tid}的掌握证据"],
        assessment_prompt=f"能不能说说{tid}？",
    )
    defaults.update(kw)
    return Topic(**defaults)


def dep(topic_id: str, prerequisite_id: str, strength: str = "hard") -> Dependency:
    return Dependency(
        topic_id=topic_id,
        prerequisite_id=prerequisite_id,
        strength=strength,
        reason=f"{prerequisite_id} 是 {topic_id} 的前置",
    )


def graph(topics=(), dependencies=(), revisits=()) -> CurriculumGraph:
    return CurriculumGraph(
        topics=list(topics),
        dependencies=list(dependencies),
        revisits=list(revisits),
    )
