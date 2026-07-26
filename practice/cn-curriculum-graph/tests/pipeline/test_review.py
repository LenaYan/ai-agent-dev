"""审核层：分歧即淘汰。

本轮定位是不承诺内容专业正确，那就宁可少产出也别放可疑的进去。
被淘汰的那批写进 dropped.json —— 它是最值得人工复核的清单，比随机抽检有价值。

⚠️ 当前双票是同族（deepseek-v4-flash + v4-pro），独立性打折。
理想是跨训练谱系互投，配上 ANTHROPIC_API_KEY 即可切换。
"""

from types import SimpleNamespace

from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, TopicDraft, Vote
from cn_curriculum_graph.pipeline.review import (
    FIDELITY_TOOL_NAME,
    DeepSeekFidelityJudge,
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
