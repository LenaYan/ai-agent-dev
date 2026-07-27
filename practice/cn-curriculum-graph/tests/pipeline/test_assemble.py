"""组装层：把流水线内部类型翻译成对外 schema，补齐代码负责的字段。

最要紧的一条：provenance 由代码填死，confidence 恒为 0.0。
自己声明自己可信是没有意义的 —— 这正是 Marble 最大的信任缺口，
本项目的立身之本就是修掉它。
"""

import pytest

from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline.assemble import assemble, dedupe_edges_by_pair, make_topic_id
from cn_curriculum_graph.pipeline.models import DraftContent, DropRecord, ProposedEdge, TopicDraft


def _draft(draft_id: str, name: str, grade: int = 4) -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id="c#001",
        standard_codes=["3.1.2"],
        content=DraftContent(
            name=name,
            description="描述",
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=grade,
            grade_end=grade,
            evidence=["证据"],
            assessment_prompt="问一句",
            source_span="原文",
        ),
    )


def test_topic_id_is_deterministic_and_prefixed():
    left, right = _draft("a", "小数的意义"), _draft("b", "小数的意义")

    assert make_topic_id(left.content) == make_topic_id(right.content)
    assert make_topic_id(left.content).startswith("t_")


def test_topic_id_differs_when_grade_differs():
    early, late = _draft("a", "认识角", grade=4), _draft("b", "认识角", grade=7)

    assert make_topic_id(early.content) != make_topic_id(late.content)


def test_provenance_is_filled_by_code_with_zero_confidence():
    graph = assemble([_draft("a", "小数的意义")], {}, model_id="deepseek-v4-flash",
                     curriculum="cn-moe-math-2022")

    prov = graph.topics[0].provenance
    assert prov.method == "llm-extract/deepseek-v4-flash"
    assert prov.review_status == "unreviewed"
    # 没人审过，任何非零置信度都是自欺
    assert prov.confidence == 0.0
    assert prov.reviewer is None


def test_standard_codes_become_standards_with_the_curriculum_constant():
    graph = assemble([_draft("a", "小数的意义")], {}, model_id="m", curriculum="cn-moe-math-2022")

    (standard,) = graph.topics[0].standards
    assert standard.curriculum == "cn-moe-math-2022"
    assert standard.code == "3.1.2"


def test_source_span_does_not_leak_into_the_output_schema():
    graph = assemble([_draft("a", "小数的意义")], {}, model_id="m", curriculum="c")

    assert "source_span" not in graph.topics[0].model_dump()


def test_edges_are_translated_using_topic_ids_not_draft_ids():
    early, late = _draft("a", "凑十", grade=3), _draft("b", "两位数加法", grade=4)
    edges = {"b": [ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="先会凑十")]}

    graph = assemble([early, late], edges, model_id="m", curriculum="c")

    (dep,) = graph.dependencies
    assert dep.topic_id == make_topic_id(late.content)
    assert dep.prerequisite_id == make_topic_id(early.content)
    assert dep.reason == "先会凑十"


def test_id_collision_raises_instead_of_silently_overwriting():
    """三项全同说明本该在 dedupe 阶段合并 —— 静默覆盖会丢数据。"""
    twin_a, twin_b = _draft("a", "小数的意义"), _draft("b", "小数的意义")

    with pytest.raises(ValueError, match="id 碰撞"):
        assemble([twin_a, twin_b], {}, model_id="m", curriculum="c")


# --- 以下两条是任务简报参考实现之外补的：assemble 的隐含前置条件是
# edges 只引用 drafts 里仍存活的 id（Task 8 编排层正是这样保证的：
# review_edges 之前先用 kept_ids 过滤过一轮）。一旦这个前置条件被违反，
# 说明调用方状态不同步（Task 4/5/6 都栽在类似的"另一份状态被改写后
# 索引失效"上），本项目的原则是"没有静默跳过"——静默过滤掉这条边等于
# 悄悄丢数据，必须在这里就炸出来，而不是拖到下游校验层才发现图不完整。


def test_edge_referencing_unknown_target_draft_raises():
    known = _draft("a", "小数的意义")
    edges = {"ghost": [ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="随便")]}

    with pytest.raises(ValueError, match="未知的 draft id"):
        assemble([known], edges, model_id="m", curriculum="c")


def test_edge_referencing_unknown_prerequisite_draft_raises():
    known = _draft("a", "小数的意义")
    edges = {"a": [ProposedEdge(prerequisite_draft_id="ghost", strength="hard", reason="随便")]}

    with pytest.raises(ValueError, match="未知的 draft id"):
        assemble([known], edges, model_id="m", curriculum="c")


# --- 审查发现：provenance 曾在循环外构造一次，所有 topic 共享同一个对象引用。
# models.py 模块 docstring 把"每节点独立记录生成方式与审核状态"列为本项目
# 相对 Marble 的刻意差异之一，而 ReviewStatus 预留了 self_reviewed /
# expert_reviewed，意味着将来要逐节点写回审核结果 —— pydantic 模型默认可变，
# 共享引用会让改一个节点的 review_status 静默污染全部节点。


def test_each_topic_gets_its_own_provenance_object():
    """每个 topic 的 provenance 必须是独立对象，不能共享同一个引用。"""
    graph = assemble(
        [_draft("a", "小数的意义"), _draft("b", "认识角", grade=5)],
        {},
        model_id="deepseek-v4-flash",
        curriculum="cn-moe-math-2022",
    )

    first, second = graph.topics[0].provenance, graph.topics[1].provenance
    assert first is not second

    for prov in (first, second):
        assert prov.method == "llm-extract/deepseek-v4-flash"
        assert prov.review_status == "unreviewed"
        assert prov.confidence == 0.0


# --- 待裁决 #5：同一 (target, prerequisite) 对给出 hard + soft 两条边，
# 两条都会进 graph.json 的 dependencies，run_all 零 finding —— 这是对外
# 产物的正确性缺陷。assemble() 本身签名不变（不加 DropRecord 通道），
# 去重放在一个独立的纯函数里，由编排层在调用 assemble 之前调用并记账
# （见 pipeline/run.py）。


def test_dedupe_edges_by_pair_keeps_hard_over_soft():
    edges = {
        "b": [
            ProposedEdge(prerequisite_draft_id="a", strength="soft", reason="有帮助"),
            ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="其实是必须的"),
        ]
    }

    deduped, drops = dedupe_edges_by_pair(edges)

    assert [e.strength for e in deduped["b"]] == ["hard"]
    assert len(drops) == 1
    assert drops[0].reason == "DUPLICATE_EDGE"
    assert drops[0].ref == "b<-a"


def test_dedupe_edges_by_pair_keeps_first_occurrence_on_tie():
    edges = {
        "b": [
            ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="第一条"),
            ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="第二条"),
        ]
    }

    deduped, drops = dedupe_edges_by_pair(edges)

    assert [e.reason for e in deduped["b"]] == ["第一条"]
    assert len(drops) == 1


def test_dedupe_edges_by_pair_leaves_unique_edges_untouched():
    edges = {
        "b": [
            ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="站得住"),
            ProposedEdge(prerequisite_draft_id="c", strength="soft", reason="也站得住"),
        ]
    }

    deduped, drops = dedupe_edges_by_pair(edges)

    assert len(deduped["b"]) == 2
    assert drops == []


def test_dedupe_edges_by_pair_three_way_tie_produces_two_distinguishable_drops(tmp_path):
    """审查者实测发现的问题：同一 (target, prerequisite) 上有 3 条同强度边时，
    dedupe_edges_by_pair 正确产出 2 条 DropRecord（保留 1 条、丢弃 2 条），
    但如果这 2 条记录的 (stage, ref, reason, detail) 四元组完全相同，
    io.append_drops 的全字段去重会把它们误判成"同一条记录被写了两次"，
    单次调用后 dropped.json 里只剩 1 条——两次真实发生的丢弃被静默合并。
    这里只调用一次 append_drops，不涉及任何重试或 checkpoint 续跑。
    """
    edges = {
        "b": [
            ProposedEdge(prerequisite_draft_id="a", strength="soft", reason="第一条理由"),
            ProposedEdge(prerequisite_draft_id="a", strength="soft", reason="第二条理由"),
            ProposedEdge(prerequisite_draft_id="a", strength="soft", reason="第三条理由"),
        ]
    }

    _, drops = dedupe_edges_by_pair(edges)
    assert len(drops) == 2  # 3 条边保留 1 条，真实丢弃 2 条

    path = tmp_path / "dropped.json"
    io.append_drops(path, drops)

    loaded = io.read_stage(path, DropRecord)
    assert len(loaded) == 2, (
        f"两次真实丢弃被 append_drops 全字段去重误折叠，dropped.json 里只剩 {len(loaded)} 条"
    )
