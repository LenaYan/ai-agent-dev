from cn_curriculum_graph.models import Revisit
from cn_curriculum_graph.validators.base import Severity
from cn_curriculum_graph.validators.ordering import (
    check_grade_monotonic,
    check_revisit_advances_grade,
)
from conftest import dep, graph, topic


def test_hard_edge_with_later_prerequisite_is_an_error():
    # 前置在 5 年级，后继却在 3 年级 —— hard 边不允许倒挂
    g = graph(
        topics=[topic("后继", grade_start=3), topic("前置", grade_start=5)],
        dependencies=[dep("后继", "前置", strength="hard")],
    )

    findings = check_grade_monotonic(g)

    assert [f.code for f in findings] == ["GRADE_INVERSION"]
    assert findings[0].severity is Severity.ERROR


def test_soft_edge_with_later_prerequisite_is_only_a_warning():
    # soft 边倒挂在真实课标里可能合理（互相支撑的概念），降级为警告
    g = graph(
        topics=[topic("后继", grade_start=3), topic("前置", grade_start=5)],
        dependencies=[dep("后继", "前置", strength="soft")],
    )

    findings = check_grade_monotonic(g)

    assert [f.code for f in findings] == ["GRADE_INVERSION"]
    assert findings[0].severity is Severity.WARNING


def test_prerequisite_in_same_grade_is_allowed():
    # 同年级内的先后顺序是常态（同一册书前后单元）
    g = graph(
        topics=[topic("后继", grade_start=4), topic("前置", grade_start=4)],
        dependencies=[dep("后继", "前置")],
    )

    assert check_grade_monotonic(g) == []


def test_revisit_edge_must_move_to_a_higher_grade():
    # 螺旋上升的定义就是"更高年级再来一轮"，同级或倒退说明边接反了
    g = graph(
        topics=[topic("分数初步", grade_start=3), topic("分数意义", grade_start=3)],
        revisits=[Revisit(earlier_id="分数初步", later_id="分数意义", note="深化")],
    )

    findings = check_revisit_advances_grade(g)

    assert [f.code for f in findings] == ["REVISIT_NOT_ADVANCING"]
    assert findings[0].severity is Severity.ERROR


def test_revisit_edge_to_higher_grade_passes():
    g = graph(
        topics=[topic("分数初步", grade_start=3), topic("分数意义", grade_start=5)],
        revisits=[Revisit(earlier_id="分数初步", later_id="分数意义", note="深化")],
    )

    assert check_revisit_advances_grade(g) == []
