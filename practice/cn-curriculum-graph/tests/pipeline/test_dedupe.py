"""去重层：规则负责缩小范围（宁可多给候选），LLM 负责下判断。

同名不同义必须显式处理 —— 实测 Marble 全量 1590 节点，28 条 NAME_DESC_MISMATCH
里 86% 落在跨年龄段复用的同名节点上。那是结构性缺陷，不是随机噪声。
"""

from types import SimpleNamespace

from cn_curriculum_graph.pipeline.dedupe import (
    SAME_TOPIC_TOOL_NAME,
    DeepSeekSameTopicJudge,
    SameTopicVerdict,
    candidate_pairs,
    dedupe,
    normalize_name,
)
from cn_curriculum_graph.pipeline.models import DraftContent, TopicDraft


def _draft(draft_id: str, name: str, *, grade: int = 4, evidence=None, desc: str = "描述") -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id=draft_id.split("-")[0],
        standard_codes=[f"3.1.{draft_id[-1]}"],
        content=DraftContent(
            name=name,
            description=desc,
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=grade,
            grade_end=grade,
            evidence=evidence or ["证据一"],
            assessment_prompt="问一句",
            source_span="原文",
        ),
    )


def test_normalize_strips_whitespace_punctuation_and_case():
    assert normalize_name(" 小数的  意义、 ") == normalize_name("小数的意义")
    assert normalize_name("Decimal Place Value") == normalize_name("decimalplacevalue")


def test_normalize_folds_fullwidth_to_halfwidth():
    assert normalize_name("小数（一）") == normalize_name("小数(一)")


def test_pairs_drafts_with_identical_normalized_names():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", " 小数的意义 "), _draft("c-3", "分数")]

    assert candidate_pairs(drafts) == [(0, 1)]


def test_pairs_drafts_with_similar_names_above_threshold():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", "小数的意义与性质")]

    assert candidate_pairs(drafts) == [(0, 1)]


def test_pairs_drafts_sharing_a_standard_code():
    """名字毫不相干，但出自同一条课标条目 —— 也要进候选。"""
    left, right = _draft("a-1", "甲概念"), _draft("b-2", "完全不同的乙")
    assert left.standard_codes != right.standard_codes  # 前提：默认编号本不相同
    right.standard_codes = list(left.standard_codes)

    assert candidate_pairs([left, right]) == [(0, 1)]


def test_unrelated_drafts_are_not_paired():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", "三角形内角和")]

    assert candidate_pairs(drafts) == []


def _judge(same_ids: set[frozenset[str]]):
    def judge(a: TopicDraft, b: TopicDraft) -> SameTopicVerdict:
        key = frozenset({a.draft_id, b.draft_id})
        return SameTopicVerdict(same=key in same_ids, reason="测试判定")

    return judge


def test_merges_take_the_draft_with_more_evidence_as_the_base():
    thin = _draft("a-1", "小数的意义", evidence=["证据一"])
    rich = _draft("b-2", "小数的意义", evidence=["证据一", "证据二"])

    result = dedupe([thin, rich], _judge({frozenset({"a-1", "b-2"})}))

    assert [d.draft_id for d in result.kept] == ["b-2"]
    assert result.merges[0].kept_draft_id == "b-2"
    assert result.merges[0].dropped_draft_id == "a-1"


def test_merge_unions_evidence_and_standard_codes():
    left = _draft("a-1", "小数的意义", evidence=["证据甲"])
    right = _draft("b-2", "小数的意义", evidence=["证据乙"])

    result = dedupe([left, right], _judge({frozenset({"a-1", "b-2"})}))

    kept = result.kept[0]
    assert set(kept.content.evidence) == {"证据甲", "证据乙"}
    assert set(kept.standard_codes) == {"3.1.1", "3.1.2"}


def test_ties_break_deterministically_by_description_then_id():
    short = _draft("b-2", "小数的意义", desc="短")
    long_desc = _draft("a-1", "小数的意义", desc="长得多的一段描述")

    result = dedupe([short, long_desc], _judge({frozenset({"a-1", "b-2"})}))

    assert [d.draft_id for d in result.kept] == ["a-1"]


def test_same_name_different_topic_gets_a_grade_qualifier():
    """同名不同义不许共存 —— 加年级限定词区分，这是 Marble 那 86% 的成因。"""
    early = _draft("a-1", "认识角", grade=4)
    late = _draft("b-2", "认识角", grade=7)

    result = dedupe([early, late], _judge(set()))  # 判为不同

    names = sorted(d.content.name for d in result.kept)
    assert names == ["认识角（4年级）", "认识角（7年级）"]


def test_same_name_same_grade_but_different_topic_drops_the_later_one():
    """加了年级还撞名，说明真的无法自动区分 —— 丢弃并记账，交给人看。"""
    first = _draft("a-1", "认识角", grade=4)
    second = _draft("b-2", "认识角", grade=4)

    result = dedupe([first, second], _judge(set()))

    assert [d.draft_id for d in result.kept] == ["a-1"]
    assert result.drops[0].reason == "SAME_NAME_DIFFERENT_TOPIC"
    assert result.drops[0].ref == "b-2"


def test_unrelated_drafts_pass_through_untouched():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", "三角形内角和")]

    result = dedupe(drafts, _judge(set()))

    assert [d.draft_id for d in result.kept] == ["a-1", "b-2"]
    assert result.merges == []
    assert result.drops == []


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", name=SAME_TOPIC_TOOL_NAME, input=tool_input)
            ]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_same_topic_judge_forces_its_tool():
    recorder: dict = {}
    client = _fake_client(recorder, {"same": True, "reason": "都是小数的意义"})

    verdict = DeepSeekSameTopicJudge(client=client)(
        _draft("a-1", "小数的意义"), _draft("b-2", "小数含义")
    )

    assert verdict.same is True
    assert recorder["tool_choice"] == {"type": "tool", "name": SAME_TOPIC_TOOL_NAME}
    assert recorder["thinking"] == {"type": "disabled"}
    assert recorder["temperature"] == 0
