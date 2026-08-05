"""误概念内容漂移的度量。

**为什么需要它**：`topic_registry` 只稳身份不稳内容。2026-07-29 那轮重跑里
「加法交换律」节点认领成功、id 没变，但它的误概念少了「对减法也成立」
那一条 —— 身份指标全绿，诊断能力却掉了。这一维此前零数据。

这些测试的重点不是"函数会算数"，而是**防止这个度量自己变成虚高的来源**：
改写不能算丢失（否则抖动虚高），一对多不能重复认领（否则留存虚高），
包含关系不能算原文未改（否则"稳定性"虚高）。
"""

import sys
from pathlib import Path

import pytest

from cn_curriculum_graph.models import CurriculumGraph, Misconception, Topic
from cn_curriculum_graph.pipeline.drift import (
    DEFAULT_MATCH_THRESHOLD,
    compare_graphs,
    diff_misconceptions,
    retention_curve,
)
from cn_curriculum_graph.serve.scoring import LiteralScorer


def _m(statement: str) -> Misconception:
    return Misconception(statement=statement, probe="随便问一句", correction_hint="往哪儿引")


def _topic(topic_id: str, name: str, statements: list[str], *, domain: str = "数与代数") -> Topic:
    return Topic(
        id=topic_id,
        name=name,
        description=f"{name}的描述",
        type="conceptual",
        subject="数学",
        domain=domain,
        grade_start=3,
        grade_end=4,
        evidence=["能举例说明"],
        assessment_prompt="你能说说吗？",
        misconceptions=[_m(s) for s in statements],
    )


def _graph(*topics: Topic) -> CurriculumGraph:
    return CurriculumGraph(topics=list(topics))


# --- 配对本身 ---------------------------------------------------------------


def test_reworded_statement_counts_as_kept_not_lost():
    """同义改写不算内容丢失 —— 否则整个抖动数字会虚高。

    名称那一层已经吃过这个亏：看起来的动荡里约七成是措辞变了而不是内容变了。
    误概念是整句自然语言，比名称更容易被重写。
    """
    before = [_m("加法有交换律，减法也有交换律")]
    after = [_m("加法有交换律，那减法应该也有交换律")]

    matched, lost, added = diff_misconceptions(before, after)

    assert lost == []
    assert added == []
    assert len(matched) == 1
    assert not matched[0].is_verbatim  # 留住了，但措辞变了 —— 这两件事要分开记账


def test_verbatim_statement_is_marked_verbatim():
    statement = "0.45 比 0.405 大，因为 45 比 405 小"
    matched, lost, added = diff_misconceptions([_m(statement)], [_m(statement)])

    assert (lost, added) == ([], [])
    assert matched[0].is_verbatim


def test_containment_scores_full_marks_but_is_not_verbatim():
    """相关度 1.00 不等于原文没变。

    `LiteralScorer` 是双向覆盖率取大者，对包含关系给满分。用"满分 = 原文相同"
    会把一次真实的截短算成没变 —— 所以 `_exact_key` 走字符串相等，不看分数。
    """
    matched, _lost, _added = diff_misconceptions(
        [_m("计数单位的感悟不牢")], [_m("计数单位")]
    )

    assert matched[0].score == pytest.approx(1.0)
    assert not matched[0].is_verbatim


def test_unrelated_statement_is_lost_not_matched():
    before = [_m("分数的分母越大，这个分数就越大")]
    after = [_m("小数点后面位数越多的小数就越大")]

    matched, lost, added = diff_misconceptions(before, after)

    assert matched == []
    assert lost == [before[0].statement]
    assert added == [after[0].statement]


def test_two_before_statements_cannot_both_claim_one_after_statement():
    """贪心一对一：两条旧误概念不许双双认领同一条新的。

    没有这条约束，留存率会被重复计数顶高 —— 而这个模块存在的意义正是
    "别造出一个好看但错误的数"。同 `topic_registry` 的一对一约束。

    **两条 before 都必须实打实高于阈值**（实测 0.833 / 0.857），否则第二条
    是被门槛挡掉的、不是被一对一挡掉的，这条测试就在守一个它没在守的东西 ——
    首版 fixture 的第二条只有 0.50，正是这种假绿。
    """
    before = [_m("减法也有交换律"), _m("减法应该有交换律")]
    after = [_m("减法应该也有交换律")]

    # 前置条件：两条都够得着门槛。写成断言而不是注释 —— 换 fixture 时它会替我
    # 抓住"第二条其实是被阈值挡掉的"这种退化。
    scorer = LiteralScorer()
    assert all(
        scorer.relevance(b.statement, after[0].statement) >= DEFAULT_MATCH_THRESHOLD
        for b in before
    )

    matched, lost, added = diff_misconceptions(before, after)

    assert len(matched) == 1
    assert len(lost) == 1
    assert added == []


def test_pairing_is_deterministic_when_scores_tie():
    """同分时的先后必须确定 —— 否则同一份输入两次跑给出不同配对，
    这个模块就把它要度量的毛病自己引进来了。"""
    before = [_m("完全一样的一条"), _m("完全一样的一条")]
    after = [_m("完全一样的一条")]

    first = diff_misconceptions(before, after)
    second = diff_misconceptions(before, after)

    assert first == second


def test_threshold_controls_how_generous_matching_is():
    before = [_m("字母只能表示一个固定的数")]
    after = [_m("字母只能代表一个具体的数，不能代表变化的量")]

    loose, loose_lost, _ = diff_misconceptions(before, after, threshold=0.3)
    strict, strict_lost, _ = diff_misconceptions(before, after, threshold=0.95)

    assert (len(loose), loose_lost) == (1, [])
    assert (strict, len(strict_lost)) == ([], 1)


# --- 图级报告 ---------------------------------------------------------------


def test_content_layer_only_counts_nodes_whose_id_survived():
    """身份没留住的节点不进内容层分母。

    节点都换身份了，说它的误概念丢了没有意义 —— 那是身份问题，
    上一层已经记过一次账。混在一起算会让一个数同时背两件事。
    """
    before = _graph(
        _topic("t_keep", "加法交换律", ["减法也有交换律"]),
        _topic("t_gone", "被换了身份的节点", ["这条不该进分母", "这条也不该"]),
    )
    after = _graph(
        _topic("t_keep", "加法交换律", ["减法也有交换律"]),
        _topic("t_new", "新身份节点", ["全新的一条"]),
    )

    report = compare_graphs(before, after)

    assert [n.topic_id for n in report.nodes] == ["t_keep"]
    assert report.statements_before == 1
    assert report.verbatim_retention == pytest.approx(1.0)
    assert report.id_jaccard == pytest.approx(1 / 3)


def test_node_identity_can_survive_while_its_misconception_disappears():
    """这个模块存在的全部理由：节点在 ≠ 误概念在。

    身份层报满分，内容层必须报出那条丢失 —— 若两层给出同一个数，
    就说明这个度量根本没在量新东西。
    """
    before = _graph(_topic("t_add", "加法交换律", ["交换两个加数的位置和不变", "减法也有交换律"]))
    after = _graph(_topic("t_add", "加法交换律", ["交换两个加数的位置和不变"]))

    report = compare_graphs(before, after)

    assert report.id_jaccard == pytest.approx(1.0)  # 身份层：完美
    assert report.matched_retention == pytest.approx(0.5)  # 内容层：掉了一半
    assert [s for _id, _name, s in report.lost_statements] == ["减法也有交换律"]
    assert report.hollowed_nodes == []  # 还剩一条，不算空壳


def test_hollowed_node_kept_its_id_and_lost_everything():
    before = _graph(_topic("t_x", "某知识点", ["旧的一条误概念"]))
    after = _graph(_topic("t_x", "某知识点", ["完全无关的新内容鸡兔同笼"]))

    report = compare_graphs(before, after)

    assert [n.topic_id for n in report.hollowed_nodes] == ["t_x"]
    assert report.matched_retention == pytest.approx(0.0)


def test_node_without_misconceptions_before_is_not_hollowed():
    """上一轮就没有误概念的节点没有可丢的东西，算进空壳只会稀释这个数。"""
    before = _graph(_topic("t_x", "某知识点", []))
    after = _graph(_topic("t_x", "某知识点", []))

    report = compare_graphs(before, after)

    assert report.hollowed_nodes == []
    assert report.statements_before == 0
    assert report.matched_retention == pytest.approx(0.0)  # 分母为 0 时不炸


def test_renamed_nodes_are_the_registry_doing_its_job():
    """id 留住、名字变了 —— 注册表被造出来就是为了这个形状。"""
    before = _graph(_topic("t_unit", "计数单位的感悟", ["旧误概念"]))
    after = _graph(_topic("t_unit", "计数单位", ["旧误概念"]))

    report = compare_graphs(before, after)

    assert [n.topic_id for n in report.renamed_nodes] == ["t_unit"]
    assert report.id_jaccard == pytest.approx(1.0)
    assert report.name_jaccard == pytest.approx(0.0)  # 名称层完全看不出这是同一个节点


def test_empty_graphs_do_not_blow_up():
    report = compare_graphs(_graph(), _graph())

    assert report.id_jaccard == 0.0
    assert report.name_jaccard == 0.0
    assert report.median_matched_score == 0.0
    assert report.statements_before == 0


# --- 阈值曲线 ---------------------------------------------------------------


def test_retention_curve_matches_pointwise_comparison():
    """曲线复用了预算的相关度矩阵 —— 复用不许改变结果。

    这正是缓存类优化最容易悄悄出错的地方：数照样出，只是不对。
    """
    before = _graph(
        _topic("t_a", "甲", ["减法也有交换律", "分母越大分数越大"]),
        _topic("t_b", "乙", ["字母只能表示一个固定的数"]),
    )
    after = _graph(
        _topic("t_a", "甲", ["减法应该也有交换律"]),
        _topic("t_b", "乙", ["字母只能表示一个固定的数"]),
    )

    thresholds = (0.3, 0.6, 0.9)
    curve = retention_curve(before, after, thresholds)

    assert [p.threshold for p in curve] == list(thresholds)
    for point in curve:
        pointwise = compare_graphs(before, after, threshold=point.threshold)
        assert point.matched_retention == pytest.approx(pointwise.matched_retention)
        assert point.hollowed == len(pointwise.hollowed_nodes)


def test_retention_is_monotonically_non_increasing_as_threshold_rises():
    """门槛越严，认领只会更少 —— 曲线若非单调，说明配对逻辑有 bug。"""
    before = _graph(_topic("t_a", "甲", ["减法也有交换律", "分母越大分数越大", "0.45 比 0.405 小"]))
    after = _graph(_topic("t_a", "甲", ["减法应该也有交换律", "分母越大分数越大", "0.45 比 0.405 大"]))

    curve = retention_curve(before, after, (0.2, 0.4, 0.6, 0.8, 0.95))
    values = [p.matched_retention for p in curve]

    assert values == sorted(values, reverse=True)


# --- CLI ---------------------------------------------------------------------


def _write(path, graph: CurriculumGraph) -> None:
    path.write_text(graph.model_dump_json(indent=2), encoding="utf-8")


def test_cli_runs_end_to_end_and_reports_both_layers(tmp_path, capsys, monkeypatch):
    """脚本会不会被属性改名悄悄打断 —— 度量逻辑全绿、CLI 一跑就炸是可能的。"""
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import compare_runs

    before = tmp_path / "before.json"
    after = tmp_path / "after.json"
    _write(before, _graph(_topic("t_add", "加法交换律", ["交换两个加数的位置和不变", "减法也有交换律"])))
    _write(after, _graph(_topic("t_add", "加法的交换律", ["交换两个加数的位置和不变"])))

    monkeypatch.setattr(sys, "argv", ["compare_runs.py", str(before), str(after)])
    assert compare_runs.main() == 0

    out = capsys.readouterr().out
    assert "id 留住但改名 = 1 个" in out
    assert "减法也有交换律" in out  # 丢失明细必须打出来，不能只报百分比
    assert "配对阈值敏感性" in out
