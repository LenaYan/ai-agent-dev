"""领域层查询：纯函数、零 key、零成本。

fixture 图刻意做成"生成图的病态形状"——带环、soft 边、revisits、孤儿、
同名节点、空图。这些不是想象出来的边界：同名节点在 Marble 全量数据上
占了 86% 的名实不符 ERROR，环则是 `review_status=unreviewed` 的图里
随时可能出现的东西（校验层只是报告它，不阻止它落盘）。
"""

import json

import pytest

from cn_curriculum_graph.models import Misconception, Provenance, Revisit
from cn_curriculum_graph.serve.query import (
    GraphIndex,
    TopicNotFoundError,
    get_graph_stats,
    get_prerequisites,
    get_topic,
    load_graph,
    match_misconceptions,
    plan_path,
    search_topics,
)
from cn_curriculum_graph.serve.scoring import normalize_math
from conftest import dep, graph, topic


def _index(*, topics=(), dependencies=(), revisits=()) -> GraphIndex:
    return GraphIndex(graph(topics=topics, dependencies=dependencies, revisits=revisits))


# --- load_graph ---------------------------------------------------------


def test_load_graph_reads_json_into_model(tmp_path):
    path = tmp_path / "graph.json"
    g = graph(topics=[topic("A"), topic("B")], dependencies=[dep("A", "B")])
    path.write_text(json.dumps(g.model_dump(mode="json"), ensure_ascii=False), encoding="utf-8")

    loaded = load_graph(path)

    assert [t.id for t in loaded.topics] == ["A", "B"]
    assert loaded.dependencies[0].prerequisite_id == "B"


def test_load_graph_rejects_malformed_graph(tmp_path):
    """坏数据要在启动时就炸，而不是等某个工具被调用时才炸。"""
    path = tmp_path / "graph.json"
    path.write_text('{"topics": [{"id": "A"}]}', encoding="utf-8")

    with pytest.raises(ValueError):
        load_graph(path)


# --- get_topic ----------------------------------------------------------


def test_get_topic_returns_the_fields_this_schema_bets_on():
    """evidence / assessment_prompt / misconceptions 是相对 Marble 的增量，
    暴露层漏掉任何一个，这个 schema 的赌注就没被验证。"""
    t = topic(
        "A",
        evidence=["能说出 0.45 里的 4 在十分位"],
        assessment_prompt="0.45 和 0.405 哪个大？",
        misconceptions=[
            Misconception(statement="位数多的大", probe="0.45 和 0.405 哪个大？", correction_hint="对齐数位")
        ],
    )
    idx = _index(topics=[t])

    detail = get_topic(idx, "A")

    assert detail.evidence == ["能说出 0.45 里的 4 在十分位"]
    assert detail.assessment_prompt == "0.45 和 0.405 哪个大？"
    assert detail.misconceptions[0].statement == "位数多的大"


def test_get_topic_fills_provenance_when_the_node_has_none():
    """provenance 缺失时不能返回 null——消费端会读成"没这回事"而不是"未审核"。"""
    idx = _index(topics=[topic("A", provenance=None)])

    detail = get_topic(idx, "A")

    assert detail.provenance.review_status == "unreviewed"
    assert detail.provenance.confidence == 0.0


def test_get_topic_keeps_the_nodes_own_provenance():
    idx = _index(
        topics=[topic("A", provenance=Provenance(method="llm-extract/deepseek-v4-flash"))]
    )

    assert get_topic(idx, "A").provenance.method == "llm-extract/deepseek-v4-flash"


def test_get_topic_unknown_id_raises_topic_not_found():
    idx = _index(topics=[topic("A")])

    with pytest.raises(TopicNotFoundError):
        get_topic(idx, "NOPE")


def test_topic_not_found_is_a_value_error():
    """按 docs/error-taxonomy.md：确定性错误必须是 ValueError，否则会被 retry_on 当成瞬时故障重试。"""
    assert issubclass(TopicNotFoundError, ValueError)


# --- search_topics ------------------------------------------------------


def test_search_matches_by_name():
    idx = _index(
        topics=[
            topic("A", name="小数的意义"),
            topic("B", name="三角形的面积"),
        ]
    )

    hits = search_topics(idx, "小数")

    assert [h.id for h in hits] == ["A"]


def test_search_matches_description_when_name_misses():
    idx = _index(
        topics=[topic("A", name="数的大小比较", description="从高位到低位逐位比较两个数的大小")]
    )

    assert [h.id for h in search_topics(idx, "逐位比较")] == ["A"]


def test_search_ranks_name_match_above_description_match():
    idx = _index(
        topics=[
            topic("A", name="分数的初步认识", description="把一个整体平均分成若干份"),
            topic("B", name="平均分", description="分数是在平均分的基础上建立的"),
        ]
    )

    assert [h.id for h in search_topics(idx, "分数")] == ["A", "B"]


def test_search_returns_empty_rather_than_forcing_a_hit():
    """"应当召回不到任何东西"是一类正样本：检索必须敢返回空。"""
    idx = _index(topics=[topic("A", name="小数的意义", description="小数在实际情境中的含义")])

    assert search_topics(idx, "光合作用") == []


def test_search_filters_by_grade():
    idx = _index(
        topics=[
            topic("A", name="分数的初步认识", grade_start=3, grade_end=3),
            topic("B", name="分数的意义", grade_start=5, grade_end=5),
        ]
    )

    assert [h.id for h in search_topics(idx, "分数", grade=5)] == ["B"]


def test_search_grade_filter_covers_the_whole_span():
    idx = _index(topics=[topic("A", name="分数", grade_start=3, grade_end=5)])

    assert [h.id for h in search_topics(idx, "分数", grade=4)] == ["A"]


def test_search_respects_limit():
    idx = _index(
        topics=[topic(f"T{i}", name=f"分数第{i}课") for i in range(5)]
    )

    assert len(search_topics(idx, "分数", limit=2)) == 2


def test_search_cards_carry_grade_so_same_name_topics_stay_distinguishable():
    """同名节点是生成图的高危区：只回名字，agent 无从分辨是哪一个。"""
    idx = _index(
        topics=[
            topic("A", name="分数的意义", grade_start=3, grade_end=3),
            topic("B", name="分数的意义", grade_start=5, grade_end=5),
        ]
    )

    hits = search_topics(idx, "分数的意义")

    assert {(h.id, h.grade_start) for h in hits} == {("A", 3), ("B", 5)}


def test_search_cards_carry_provenance():
    idx = _index(topics=[topic("A", name="小数的意义")])

    assert search_topics(idx, "小数")[0].provenance.review_status == "unreviewed"


def test_search_on_empty_graph_returns_empty():
    assert search_topics(_index(), "分数") == []


def test_index_exposes_revisits_without_choking_on_them():
    """空图与带 revisits 的图都要能建索引——建索引阶段炸掉，六个工具全废。"""
    idx = _index(
        topics=[topic("A", grade_start=3), topic("B", grade_start=5)],
        revisits=[Revisit(earlier_id="A", later_id="B", note="从初步认识到意义")],
    )

    assert idx.revisits_of("A")[0].later_id == "B"
    assert idx.revisits_of("B")[0].earlier_id == "A"
    assert idx.revisits_of("A") == idx.revisits_of("B")


# --- match_misconceptions -----------------------------------------------

_DECIMAL_MISCONCEPTION = Misconception(
    statement="「0.45 比 0.405 小，因为 45 比 405 小」",
    probe="0.45 和 0.405 哪个大？为什么？",
    correction_hint="引导孩子对齐数位：十分位 4 相同，百分位 5 大于 0。",
)


def test_match_misconceptions_returns_the_full_misconception_triple():
    """statement / probe / correction_hint 三件套是这个工具存在的全部理由：
    少任何一个，agent 就只能知道"孩子错了"而没法确认与纠正。"""
    idx = _index(topics=[topic("A", name="小数的大小比较", misconceptions=[_DECIMAL_MISCONCEPTION])])

    hits = match_misconceptions(idx, "孩子说 0.45 比 0.405 小")

    assert len(hits) == 1
    assert hits[0].topic_id == "A"
    assert hits[0].topic_name == "小数的大小比较"
    assert hits[0].statement == _DECIMAL_MISCONCEPTION.statement
    assert hits[0].probe == _DECIMAL_MISCONCEPTION.probe
    assert hits[0].correction_hint == _DECIMAL_MISCONCEPTION.correction_hint


def test_match_misconceptions_skips_topics_without_any():
    idx = _index(
        topics=[
            topic("A", name="小数的大小比较", misconceptions=[_DECIMAL_MISCONCEPTION]),
            topic("B", name="小数的大小", misconceptions=[]),
        ]
    )

    assert [h.topic_id for h in match_misconceptions(idx, "0.45 比 0.405 小")] == ["A"]


def test_match_misconceptions_can_hit_via_the_correction_hint():
    """家长的说法未必贴着 statement 的措辞，correction_hint / probe 是第二条入口。"""
    idx = _index(topics=[topic("A", misconceptions=[_DECIMAL_MISCONCEPTION])])

    assert [h.topic_id for h in match_misconceptions(idx, "数位对齐")] == ["A"]


def test_match_misconceptions_ranks_statement_hits_first():
    other = Misconception(
        statement="「分子分母都加 1，分数大小不变」",
        probe="1/2 和 2/3 一样大吗？",
        correction_hint="用画图看两块面积。0.45 这类小数问题不在这里。",
    )
    idx = _index(
        topics=[
            topic("A", name="分数的意义", misconceptions=[other]),
            topic("B", name="小数的大小比较", misconceptions=[_DECIMAL_MISCONCEPTION]),
        ]
    )

    assert [h.topic_id for h in match_misconceptions(idx, "0.45 比 0.405 小")][0] == "B"


def test_match_misconceptions_returns_empty_when_nothing_is_close():
    idx = _index(topics=[topic("A", misconceptions=[_DECIMAL_MISCONCEPTION])])

    assert match_misconceptions(idx, "孩子不爱吃青菜") == []


def test_match_misconceptions_respects_limit():
    idx = _index(
        topics=[
            topic(f"T{i}", misconceptions=[_DECIMAL_MISCONCEPTION]) for i in range(4)
        ]
    )

    assert len(match_misconceptions(idx, "0.45 比 0.405 小", limit=2)) == 2


def test_match_misconceptions_carries_provenance_and_grade():
    idx = _index(
        topics=[topic("A", grade_start=4, grade_end=4, misconceptions=[_DECIMAL_MISCONCEPTION])]
    )

    hit = match_misconceptions(idx, "0.45 比 0.405 小")[0]

    assert hit.grade_start == 4
    assert hit.provenance.review_status == "unreviewed"


def test_match_misconceptions_on_empty_graph_returns_empty():
    assert match_misconceptions(_index(), "0.45 比 0.405 小") == []


# --- get_graph_stats ----------------------------------------------------


def test_stats_count_nodes_and_edges_by_strength():
    idx = _index(
        topics=[topic("A"), topic("B"), topic("C", grade_start=5)],
        dependencies=[dep("A", "B"), dep("A", "C", strength="soft")],
        revisits=[Revisit(earlier_id="B", later_id="C", note="深化")],
    )

    stats = get_graph_stats(idx)

    assert stats.topic_count == 3
    assert stats.dependency_count == 2
    assert stats.hard_dependency_count == 1
    assert stats.soft_dependency_count == 1
    assert stats.revisit_count == 1


def test_stats_grade_distribution_counts_every_grade_a_topic_spans():
    idx = _index(topics=[topic("A", grade_start=2, grade_end=4), topic("B", grade_start=4)])

    assert get_graph_stats(idx).grade_distribution == {2: 1, 3: 1, 4: 2}


def test_stats_report_covered_standard_codes():
    from cn_curriculum_graph.models import Standard

    idx = _index(
        topics=[
            topic("A", standards=[Standard(curriculum="cn-moe-math-2022", code="1.1.1")]),
            topic("B", standards=[Standard(curriculum="cn-moe-math-2022", code="1.1.1")]),
            topic("C", standards=[Standard(curriculum="cn-moe-math-2022", code="1.2.3")]),
        ]
    )

    assert get_graph_stats(idx).standard_codes == {"cn-moe-math-2022": ["1.1.1", "1.2.3"]}


def test_stats_expose_how_thin_the_graph_really_is():
    """孤儿数与最长前置链是这个工具存在的理由：防止 agent 在一张
    41% 孤立、最长链 3 层的图上假装自己在做课程规划。"""
    idx = _index(
        topics=[topic("A"), topic("B"), topic("C"), topic("ORPHAN")],
        dependencies=[dep("A", "B"), dep("B", "C")],
    )

    stats = get_graph_stats(idx)

    assert stats.isolated_topic_count == 1
    assert stats.longest_prerequisite_chain == 3


def test_stats_do_not_hang_on_a_cyclic_graph():
    """图是 unreviewed 的，校验层只报告环、不阻止它落盘。"""
    idx = _index(
        topics=[topic("A"), topic("B"), topic("C")],
        dependencies=[dep("A", "B"), dep("B", "C"), dep("C", "A")],
    )

    stats = get_graph_stats(idx)

    assert stats.has_cycle is True


def test_stats_report_review_status_breakdown():
    idx = _index(
        topics=[
            topic("A", provenance=Provenance(method="llm-extract/x")),
            topic("B", provenance=None),
        ]
    )

    assert get_graph_stats(idx).review_status_counts == {"unreviewed": 2}


def test_stats_on_empty_graph_are_all_zero():
    stats = get_graph_stats(_index())

    assert stats.topic_count == 0
    assert stats.longest_prerequisite_chain == 0
    assert stats.has_cycle is False
    assert stats.grade_distribution == {}


# --- get_prerequisites --------------------------------------------------


def test_prerequisites_carry_strength_and_reason():
    """strength/reason 是 dependencies 相对"一条无标注的边"的全部增量：
    reason 就是给 agent 直接拿去讲"为什么要先学这个"的。"""
    idx = _index(topics=[topic("A"), topic("B")], dependencies=[dep("A", "B")])

    prereqs = get_prerequisites(idx, "A")

    assert [(p.topic_id, p.strength, p.depth) for p in prereqs] == [("B", "hard", 1)]
    assert prereqs[0].reason == "B 是 A 的前置"
    assert prereqs[0].required_by == "A"


def test_prerequisites_walk_up_to_the_requested_depth():
    idx = _index(
        topics=[topic("A"), topic("B"), topic("C")],
        dependencies=[dep("A", "B"), dep("B", "C")],
    )

    assert [(p.topic_id, p.depth) for p in get_prerequisites(idx, "A", depth=2)] == [
        ("B", 1),
        ("C", 2),
    ]
    assert [p.topic_id for p in get_prerequisites(idx, "A", depth=1)] == ["B"]


def test_prerequisites_can_drop_soft_edges():
    """hard 是"必须先会"，soft 是"最好先会"——要不要算，是消费端的产品决策。"""
    idx = _index(
        topics=[topic("A"), topic("B"), topic("C")],
        dependencies=[dep("A", "B"), dep("A", "C", strength="soft")],
    )

    assert {p.topic_id for p in get_prerequisites(idx, "A")} == {"B", "C"}
    assert [p.topic_id for p in get_prerequisites(idx, "A", include_soft=False)] == ["B"]


def test_prerequisites_of_an_orphan_is_empty():
    idx = _index(topics=[topic("ORPHAN")])

    assert get_prerequisites(idx, "ORPHAN") == []


def test_prerequisites_terminate_on_a_cycle():
    idx = _index(
        topics=[topic("A"), topic("B"), topic("C")],
        dependencies=[dep("A", "B"), dep("B", "C"), dep("C", "A")],
    )

    prereqs = get_prerequisites(idx, "A", depth=10)

    assert [p.topic_id for p in prereqs] == ["B", "C"]


def test_prerequisites_of_unknown_topic_raises():
    with pytest.raises(TopicNotFoundError):
        get_prerequisites(_index(topics=[topic("A")]), "NOPE")


def test_prerequisites_skip_dangling_refs_instead_of_crashing():
    """悬挂引用由校验层报告，暴露层的职责是别把它变成 500。"""
    idx = GraphIndex(graph(topics=[topic("A")], dependencies=[dep("A", "GONE")]))

    assert get_prerequisites(idx, "A") == []


# --- plan_path ----------------------------------------------------------


def _chain_index() -> GraphIndex:
    """整数加法 → 小数加法 → 分数加法，外加一条 soft 旁支。"""
    return _index(
        topics=[
            topic("int_add", name="整数加法", grade_start=1),
            topic("dec_add", name="小数加法", grade_start=4),
            topic("frac_add", name="分数加法", grade_start=5),
            topic("estimate", name="估算", grade_start=3),
        ],
        dependencies=[
            dep("frac_add", "dec_add"),
            dep("dec_add", "int_add"),
            dep("frac_add", "estimate", strength="soft"),
        ],
    )


def test_plan_path_orders_prerequisites_before_the_target():
    plan = plan_path(_chain_index(), "frac_add", include_soft=False)

    assert [s.topic_id for s in plan.steps] == ["int_add", "dec_add", "frac_add"]
    assert [s.order for s in plan.steps] == [1, 2, 3]


def test_plan_path_includes_soft_branches_when_asked():
    plan = plan_path(_chain_index(), "frac_add")

    assert set(s.topic_id for s in plan.steps) == {"int_add", "estimate", "dec_add", "frac_add"}
    assert plan.steps[-1].topic_id == "frac_add"


def test_known_ids_drop_the_topic_and_everything_upstream_of_it():
    """定死的语义："会了 X"蕴含"会了 X 的前置"。
    另一种读法（只剔除 id 本身）会在孩子已经会小数加法时还让他从整数加法学起。"""
    plan = plan_path(_chain_index(), "frac_add", known_ids=["dec_add"], include_soft=False)

    assert [s.topic_id for s in plan.steps] == ["frac_add"]
    assert set(plan.skipped_known) == {"dec_add", "int_add"}


def test_known_ids_that_are_irrelevant_change_nothing():
    plan = plan_path(_chain_index(), "frac_add", known_ids=["estimate"], include_soft=False)

    assert [s.topic_id for s in plan.steps] == ["int_add", "dec_add", "frac_add"]


def test_known_target_yields_an_empty_path():
    plan = plan_path(_chain_index(), "frac_add", known_ids=["frac_add"])

    assert plan.steps == []


def test_plan_path_attaches_related_revisits():
    """revisits 是 schema 相对扁平 DAG 的增量：同一概念在更高年级再来一轮，
    规划时要让 agent 知道"这个还会再遇到"。"""
    idx = _index(
        topics=[topic("frac_1", grade_start=3), topic("frac_2", grade_start=5)],
        dependencies=[dep("frac_2", "frac_1")],
        revisits=[Revisit(earlier_id="frac_1", later_id="frac_2", note="从初步认识到意义")],
    )

    plan = plan_path(idx, "frac_2")

    first = plan.steps[0]
    assert first.topic_id == "frac_1"
    assert first.revisits[0].counterpart_id == "frac_2"
    assert first.revisits[0].direction == "later"
    assert first.revisits[0].note == "从初步认识到意义"


def test_plan_path_terminates_and_flags_a_cycle():
    """带环的图上不能死循环，也不能假装排出了一个合法学习序列。"""
    idx = _index(
        topics=[topic("A"), topic("B"), topic("C")],
        dependencies=[dep("A", "B"), dep("B", "C"), dep("C", "A")],
    )

    plan = plan_path(idx, "A")

    assert plan.has_cycle is True
    assert sorted(s.topic_id for s in plan.steps) == ["A", "B", "C"]


def test_plan_path_for_an_orphan_is_just_itself():
    plan = plan_path(_index(topics=[topic("ORPHAN")]), "ORPHAN")

    assert [s.topic_id for s in plan.steps] == ["ORPHAN"]
    assert plan.has_cycle is False


def test_plan_path_steps_carry_provenance():
    plan = plan_path(_chain_index(), "frac_add")

    assert all(s.provenance.review_status == "unreviewed" for s in plan.steps)


def test_plan_path_unknown_target_raises():
    with pytest.raises(TopicNotFoundError):
        plan_path(_index(topics=[topic("A")]), "NOPE")


def test_plan_path_unknown_known_id_raises():
    """已掌握列表里写错 id 必须报错——静默忽略会让"跳过了什么"变成猜谜。"""
    with pytest.raises(TopicNotFoundError):
        plan_path(_chain_index(), "frac_add", known_ids=["typo"])


# --- 字面匹配的两处结构性缺陷（都是真实评测逼出来的） -------------------


def test_a_long_observation_can_match_a_short_misconception():
    """长度惩罚：家长说得越具体越啰嗦，覆盖率的分母就越大。

    只按"查询被覆盖了多少"打分，会让一句完整的家长转述永远够不到门槛
    —— 实测 recall@3 因此卡在 63%。命中可以来自任一方向：
    观察句覆盖了这条误概念的特征，同样算命中。
    """
    idx = _index(
        topics=[
            topic(
                "A",
                misconceptions=[
                    Misconception(
                        statement="减法也有交换律，比如 5-3=3-5",
                        probe="5-3 和 3-5 一样吗？",
                        correction_hint="让孩子实际算一算。",
                    )
                ],
            )
        ]
    )

    hits = match_misconceptions(idx, "他说减法也能交换，5 减 3 和 3 减 5 反正结果一样，写哪个都行。")

    assert [h.topic_id for h in hits] == ["A"]


def test_spoken_chinese_matches_math_notation():
    """表示层不同：图里写 1/8，家长说"八分之一"，字面重叠为零。

    这不是同义词问题（加权、阈值都救不了），是同一个数学对象的两种写法。
    归一化两边即可，仍然不需要模型。
    """
    idx = _index(
        topics=[
            topic(
                "A",
                misconceptions=[
                    Misconception(
                        statement="1/8 比 1/5 大，因为 8 比 5 大",
                        probe="哪个更大？",
                        correction_hint="分的份数越多每份越小。",
                    )
                ],
            )
        ]
    )

    hits = match_misconceptions(idx, "孩子坚持八分之一比五分之一大，理由是 8 比 5 大。")

    assert [h.topic_id for h in hits] == ["A"]


def test_normalization_leaves_operator_words_alone():
    """只在数字之间改写运算符：把"减法"改成"-法"会伤到一大批正常词。"""
    assert normalize_math("减法也有交换律") == "减法也有交换律"
    assert normalize_math("5 减 3") == "5-3"
    assert normalize_math("八分之一") == "1/8"
    assert normalize_math("二分之一加五分之一") == "1/2+1/5"


def test_short_names_do_not_swallow_unrelated_queries():
    """目标侧覆盖率是把双刃剑：短 name 容易被"完全覆盖"。
    这条钉住它不会退化成"什么都命中"。"""
    idx = _index(topics=[topic("A", name="平均分", description="把一个整体分成同样多的几份")])

    assert search_topics(idx, "孩子背古诗老记不住") == []
