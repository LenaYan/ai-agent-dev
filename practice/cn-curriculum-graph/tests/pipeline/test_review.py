"""审核层：分歧即淘汰。

本轮定位是不承诺内容专业正确，那就宁可少产出也别放可疑的进去。
被淘汰的那批写进 dropped.json —— 它是最值得人工复核的清单，比随机抽检有价值。

⚠️ 当前双票是同族（deepseek-v4-flash + v4-pro），独立性打折。
理想是跨训练谱系互投，配上 ANTHROPIC_API_KEY 即可切换。
"""

from types import SimpleNamespace

import pytest

from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, TopicDraft, Vote
from cn_curriculum_graph.pipeline.review import (
    FIDELITY_TOOL_NAME,
    DeepSeekFidelityJudge,
    detect_orphans,
    filter_edges_by_kept_drafts,
    review_drafts,
    review_edges,
)
from cn_curriculum_graph.validators.consistency import Verdict


def _draft(draft_id: str = "a", name: str = "小数的意义") -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id="c#001",
        standard_codes=["3.1.2"],
        content=DraftContent(
            name=name,
            description="理解小数表示十进制分数",
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=4,
            grade_end=4,
            evidence=["证据"],
            assessment_prompt="问一句",
            source_span="能理解小数的意义",
        ),
    )


def _fidelity(approved: bool, reviewer: str = "fake"):
    def judge(draft: TopicDraft) -> Vote:
        return Vote(reviewer=reviewer, approved=approved, reason="测试")

    return judge


def _name_judge(judgment: str):
    def judge(name: str, description: str) -> Verdict:
        return Verdict(judgment=judgment, reason="测试")

    return judge


def test_a_draft_approved_by_everyone_is_kept():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲"), _fidelity(True, "乙")],
        name_judges=[_name_judge("consistent")],
    )

    assert [d.draft_id for d in result.kept] == ["a"]
    assert result.drops == []


def test_split_fidelity_vote_drops_the_draft():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲"), _fidelity(False, "乙")],
        name_judges=[_name_judge("consistent")],
    )

    assert result.kept == []
    assert result.drops[0].reason == "REVIEW_REJECTED"
    assert "fidelity" in result.drops[0].detail


def test_topic_mismatch_drops_the_draft():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True)],
        name_judges=[_name_judge("topic_mismatch")],
    )

    assert result.kept == []
    assert "name_desc" in result.drops[0].detail


def test_scope_mismatch_keeps_the_draft_but_records_the_outcome():
    """范围不符是 WARNING 级 —— 保留，但要留痕，跟校验层的两级严重性一致。"""
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True)],
        name_judges=[_name_judge("scope_mismatch")],
    )

    assert [d.draft_id for d in result.kept] == ["a"]
    scoped = [o for o in result.outcomes if o.aspect == "name_desc"]
    assert scoped[0].approved is True
    assert "scope_mismatch" in scoped[0].votes[0].reason


def test_every_decision_is_recorded_even_when_approved():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲")],
        name_judges=[_name_judge("consistent")],
    )

    assert {o.aspect for o in result.outcomes} == {"fidelity", "name_desc"}


def test_edges_are_reviewed_and_rejected_ones_dropped():
    def approve(target, edge):
        return Vote(reviewer="甲", approved=edge.prerequisite_draft_id == "good", reason="测试")

    edges = {
        "a": [
            ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住"),
            ProposedEdge(prerequisite_draft_id="bad", strength="soft", reason="站不住"),
        ]
    }

    result = review_edges({"a": _draft("a")}, edges, edge_judges=[approve])

    assert [e.prerequisite_draft_id for e in result.kept_edges["a"]] == ["good"]
    assert result.drops[0].reason == "REVIEW_REJECTED"
    assert "bad" in result.drops[0].detail


def test_fidelity_judge_failure_drops_the_draft_conservatively_without_crashing():
    """设计文档 §6：单条目失败不中断整批。review 是全流水线调用量最高的一层
    （约为 extract 的 4-6 倍），任意一次限流/超时都会撞上；一次抛异常绝不能
    让 run_pipeline 整体崩掉、丢光前几层的 LLM 花费。判定器没能表态时，
    按本层"分歧即淘汰"的精神保守处理：该草稿判为未通过，而不是放行。"""

    def flaky_fidelity(draft: TopicDraft) -> Vote:
        if draft.draft_id == "a":
            raise RuntimeError("429")
        return Vote(reviewer="fake", approved=True, reason="ok")

    other_draft = _draft("b", "另一个知识点")
    result = review_drafts(
        [_draft("a"), other_draft],
        fidelity_judges=[flaky_fidelity],
        name_judges=[_name_judge("consistent")],
    )

    # 出问题的那条被保守淘汰，记账原因码专门区分于普通 REVIEW_REJECTED
    assert "a" not in {d.draft_id for d in result.kept}
    fail_drops = [d for d in result.drops if d.reason == "FIDELITY_JUDGE_FAILED"]
    assert len(fail_drops) == 1
    assert fail_drops[0].ref == "a"
    assert "RuntimeError" in fail_drops[0].detail

    # 其余草稿不受影响，照常评审通过
    assert "b" in {d.draft_id for d in result.kept}


def test_name_judge_failure_drops_the_draft_conservatively_without_crashing():
    def flaky_name_judge(name: str, description: str):
        raise RuntimeError("timeout")

    result = review_drafts(
        [_draft("a")],
        fidelity_judges=[_fidelity(True)],
        name_judges=[flaky_name_judge],
    )

    assert result.kept == []
    fail_drops = [d for d in result.drops if d.reason == "NAME_JUDGE_FAILED"]
    assert len(fail_drops) == 1
    assert fail_drops[0].ref == "a"
    assert "RuntimeError" in fail_drops[0].detail


def test_edge_judge_failure_drops_the_edge_conservatively_without_crashing():
    def flaky_edge_judge(target, edge):
        raise RuntimeError("429")

    edges = {
        "a": [ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住")],
    }

    result = review_edges({"a": _draft("a")}, edges, edge_judges=[flaky_edge_judge])

    assert result.kept_edges["a"] == []
    fail_drops = [d for d in result.drops if d.reason == "EDGE_JUDGE_FAILED"]
    assert len(fail_drops) == 1
    assert "a<-good" in fail_drops[0].ref
    assert "RuntimeError" in fail_drops[0].detail


def test_review_reraises_programming_errors_instead_of_recording_a_drop():
    """AttributeError/TypeError/NameError/KeyError 是程序 bug，不该被
    FIDELITY_JUDGE_FAILED/NAME_JUDGE_FAILED/EDGE_JUDGE_FAILED 悄悄吞掉。"""
    import pytest

    def buggy_fidelity(draft: TopicDraft) -> Vote:
        raise AttributeError("拼错了属性名")

    with pytest.raises(AttributeError):
        review_drafts(
            [_draft()], fidelity_judges=[buggy_fidelity], name_judges=[_name_judge("consistent")]
        )

    def buggy_name_judge(name: str, description: str):
        raise TypeError("参数不对")

    with pytest.raises(TypeError):
        review_drafts(
            [_draft()], fidelity_judges=[_fidelity(True)], name_judges=[buggy_name_judge]
        )

    def buggy_edge_judge(target, edge):
        raise KeyError("some_key")

    edges = {"a": [ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住")]}
    with pytest.raises(KeyError):
        review_edges({"a": _draft("a")}, edges, edge_judges=[buggy_edge_judge])


def test_review_edges_skips_unknown_target_without_crashing():
    """预先算好的 drafts_by_id 与 edges 字典键不一定同步（例如上游只传审核后
    幸存的 draft，edges 里却还留着已被淘汰目标的边）。查不到目标不能让整层
    崩掉（KeyError），也不能静默丢 —— 必须留一条带原因码的 DropRecord。"""

    edges = {
        "ghost": [ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="站得住")],
    }

    result = review_edges(
        {},
        edges,
        edge_judges=[lambda target, edge: Vote(reviewer="甲", approved=True, reason="测试")],
    )

    assert result.kept_edges == {}
    assert result.drops[0].reason == "UNKNOWN_REVIEW_TARGET"
    assert "ghost" in result.drops[0].detail


def test_review_drafts_rejects_empty_fidelity_judges():
    """空 judges 列表不是合法输入而是配置错误：all([]) == True 会把零信息
    读成满票通过，与"分歧即淘汰"的设计哲学正好相反，必须当场炸而不是静默放行。"""
    with pytest.raises(ValueError, match="fidelity_judges"):
        review_drafts(
            [_draft()],
            fidelity_judges=[],
            name_judges=[_name_judge("consistent")],
        )


def test_review_drafts_rejects_empty_name_judges():
    with pytest.raises(ValueError, match="name_judges"):
        review_drafts(
            [_draft()],
            fidelity_judges=[_fidelity(True)],
            name_judges=[],
        )


def test_review_edges_rejects_empty_edge_judges():
    edges = {
        "a": [ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住")],
    }

    with pytest.raises(ValueError, match="edge_judges"):
        review_edges({"a": _draft("a")}, edges, edge_judges=[])


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", name=FIDELITY_TOOL_NAME, input=tool_input)
            ]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_fidelity_judge_shows_both_description_and_source_span():
    recorder: dict = {}
    client = _fake_client(recorder, {"approved": False, "reason": "描述超出原文"})

    vote = DeepSeekFidelityJudge(client=client, model="deepseek-v4-pro")(_draft())

    assert vote.approved is False
    assert vote.reviewer == "deepseek-v4-pro"
    payload = str(recorder["messages"])
    assert "理解小数表示十进制分数" in payload
    assert "能理解小数的意义" in payload
    assert recorder["tool_choice"] == {"type": "tool", "name": FIDELITY_TOOL_NAME}


def test_filter_edges_records_target_rejected():
    proposed = {"a": [ProposedEdge(prerequisite_draft_id="b", strength="hard", reason="因")]}

    surviving, drops = filter_edges_by_kept_drafts(proposed, kept_ids={"b"})

    assert surviving == {}
    assert drops[0].reason == "EDGE_TARGET_REJECTED"
    assert drops[0].ref == "a<-b"


def test_filter_edges_records_prerequisite_rejected():
    proposed = {"a": [ProposedEdge(prerequisite_draft_id="b", strength="hard", reason="因")]}

    surviving, drops = filter_edges_by_kept_drafts(proposed, kept_ids={"a"})

    assert surviving == {"a": []}
    assert drops[0].reason == "EDGE_PREREQUISITE_REJECTED"
    assert drops[0].ref == "a<-b"


def test_filter_edges_keeps_both_ends_alive():
    proposed = {"a": [ProposedEdge(prerequisite_draft_id="b", strength="hard", reason="因")]}

    surviving, drops = filter_edges_by_kept_drafts(proposed, kept_ids={"a", "b"})

    assert [e.prerequisite_draft_id for e in surviving["a"]] == ["b"]
    assert drops == []


def test_detect_orphans_flags_a_draft_that_lost_all_prerequisites():
    """基础节点被淘汰后，后继静默失去全部前置 —— 真实运行实测到的语义缺口。"""
    survivor = _draft("child")
    proposed_before = {"child": [ProposedEdge(prerequisite_draft_id="base", strength="hard", reason="因")]}

    drops = detect_orphans([survivor], proposed_before, kept_edges={"child": []})

    assert len(drops) == 1
    assert drops[0].reason == "ORPHANED_BY_REJECTION"
    assert drops[0].ref == "child"
    assert "base" in drops[0].detail


def test_detect_orphans_ignores_drafts_that_never_had_prerequisites():
    """最低年级节点本来就没有前置，不算孤儿 —— 否则每次跑都刷一堆噪声。"""
    survivor = _draft("root")

    assert detect_orphans([survivor], proposed_before={"root": []}, kept_edges={"root": []}) == []


def test_detect_orphans_ignores_drafts_that_kept_at_least_one_prerequisite():
    survivor = _draft("child")
    proposed_before = {
        "child": [
            ProposedEdge(prerequisite_draft_id="gone", strength="hard", reason="因"),
            ProposedEdge(prerequisite_draft_id="alive", strength="soft", reason="因"),
        ]
    }
    kept = {"child": [ProposedEdge(prerequisite_draft_id="alive", strength="soft", reason="因")]}

    assert detect_orphans([survivor], proposed_before, kept_edges=kept) == []
