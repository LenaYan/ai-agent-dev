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


def test_three_same_name_drafts_all_different_grades_all_get_qualifiers():
    """三个同名草稿年级互不相同 —— 全部加限定词区分，无一被丢弃。"""
    a = _draft("a-1", "认识角", grade=4)
    b = _draft("b-2", "认识角", grade=7)
    c = _draft("c-3", "认识角", grade=6)

    result = dedupe([a, b, c], _judge(set()))  # 所有对都判为不同

    names = sorted(d.content.name for d in result.kept)
    assert names == ["认识角（4年级）", "认识角（6年级）", "认识角（7年级）"]
    assert result.drops == []


def test_three_same_name_drafts_two_share_a_grade():
    """三个同名，其中两个同年级 —— 同年级里留 draft_id 小者、丢另一个并记账；
    第三个不同年级的加限定词保留。

    这正是评审者实测出的「三方同名裸名逃逸」缺陷复现用例：candidate_pairs
    产出 (0,1)(0,2)(1,2)，旧实现处理完 (0,1) 后 a-1/b-2 都已改名，轮到 (0,2) 时
    归一化名不再相等就直接放过，导致 c-3 带着裸名「认识角」逃逸、drops 为空。
    """
    a = _draft("a-1", "认识角", grade=4)
    b = _draft("b-2", "认识角", grade=7)
    c = _draft("c-3", "认识角", grade=4)

    result = dedupe([a, b, c], _judge(set()))

    kept_ids = {d.draft_id for d in result.kept}
    assert kept_ids == {"a-1", "b-2"}
    assert "c-3" not in kept_ids  # 旧实现里这是逃逸的裸名节点

    names = {d.draft_id: d.content.name for d in result.kept}
    assert names == {"a-1": "认识角（4年级）", "b-2": "认识角（7年级）"}

    assert len(result.drops) == 1
    assert result.drops[0].reason == "SAME_NAME_DIFFERENT_TOPIC"
    assert result.drops[0].ref == "c-3"


def test_four_same_name_drafts_two_pairs_share_grades():
    """四个同名，两两同年级（4/4/7/7）—— 每对各留 draft_id 最小者，其余丢弃记账。"""
    a = _draft("a-1", "认识角", grade=4)
    b = _draft("b-2", "认识角", grade=4)
    c = _draft("c-3", "认识角", grade=7)
    d = _draft("d-4", "认识角", grade=7)

    result = dedupe([a, b, c, d], _judge(set()))

    kept_ids = {x.draft_id for x in result.kept}
    assert kept_ids == {"a-1", "c-3"}

    names = {x.draft_id: x.content.name for x in result.kept}
    assert names == {"a-1": "认识角（4年级）", "c-3": "认识角（7年级）"}

    dropped_ids = {rec.ref for rec in result.drops}
    assert dropped_ids == {"b-2", "d-4"}
    assert all(rec.reason == "SAME_NAME_DIFFERENT_TOPIC" for rec in result.drops)


def test_judge_exception_records_a_drop_and_lets_other_pairs_proceed():
    """一对 judge 调用抛异常 —— 该对不合并（两个都保留），记 SAME_TOPIC_JUDGE_FAILED，
    其余候选对照常处理，不因一次异常中断整批（设计文档 §6 的批处理容错要求，
    对齐 extract.py 的 EXTRACT_FAILED 模式）。

    用共享 standard_code 强制配对，而非同名，避免和阶段二的同名消歧逻辑产生干扰。
    """
    flaky_left = _draft("a-1", "甲概念")
    flaky_right = _draft("b-2", "完全不同的乙")
    flaky_right.standard_codes = list(flaky_left.standard_codes)  # 强制配对

    merge_left = _draft("c-3", "小数的意义")
    merge_right = _draft("d-4", "小数的意义")

    def flaky_judge(x: TopicDraft, y: TopicDraft) -> SameTopicVerdict:
        key = frozenset({x.draft_id, y.draft_id})
        if key == frozenset({"a-1", "b-2"}):
            raise RuntimeError("judge 服务超时")
        return SameTopicVerdict(same=True, reason="确实是同一个")

    result = dedupe([flaky_left, flaky_right, merge_left, merge_right], flaky_judge)

    # 异常那对：不合并，两个都保留
    kept_ids = {d.draft_id for d in result.kept}
    assert {"a-1", "b-2"}.issubset(kept_ids)

    # 其余候选对不受影响，照常合并
    assert len(result.merges) == 1
    assert result.merges[0].kept_draft_id in {"c-3", "d-4"}

    # 异常有对应记账
    judge_fail_drops = [d for d in result.drops if d.reason == "SAME_TOPIC_JUDGE_FAILED"]
    assert len(judge_fail_drops) == 1
    assert judge_fail_drops[0].ref in {"a-1", "b-2"}
    assert "RuntimeError" in judge_fail_drops[0].detail


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
