"""去重：同一知识点常被多条课标条目提到。

分工刻意如此 —— **规则只负责缩小范围，宁可多给候选**，判断交给 LLM。
反过来（规则直接判同一）会把"小数的意义"和"小数的意义与性质"这种
需要语义才能分辨的情形判错。
"""

from __future__ import annotations

import difflib
import os
import re
import unicodedata
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import DropRecord, Merge, TopicDraft

DEFAULT_MODEL = "deepseek-v4-flash"
SAME_TOPIC_TOOL_NAME = "record_same_topic"
SIMILARITY_THRESHOLD = 0.85

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_name(name: str) -> str:
    """去空白、去标点、转小写、全角转半角。只用于配对，不改原始 name。"""
    folded = unicodedata.normalize("NFKC", name).lower()
    return _PUNCT.sub("", folded)


def _containment_ratio(a: str, b: str) -> float:
    """相似度用「重叠字符数 / 较短串长度」而非 SequenceMatcher.ratio()。

    要抓的典型情形是"名称被扩写"，如"小数的意义" -> "小数的意义与性质"：
    前者整段是后者的子串。但 ratio() 的分母是两串长度之和，数学上被
    2*min(len)/(len_a+len_b) 封顶 —— 5 字 vs 8 字最高只能到 0.77，永远
    到不了 0.85 阈值，即便完全包含。用较短串长度做分母才能让"扩写"场景
    正确落在阈值之上，同时不相关的名称重叠字符少，仍然会被挡在阈值外。
    """
    shorter = min(len(a), len(b))
    if shorter == 0:
        return 0.0
    matcher = difflib.SequenceMatcher(None, a, b)
    overlap = sum(block.size for block in matcher.get_matching_blocks())
    return overlap / shorter


def candidate_pairs(drafts: list[TopicDraft]) -> list[tuple[int, int]]:
    """满足任一即进候选：归一化名相同、相似度 ≥ 阈值、条目编号有交集。"""
    pairs: list[tuple[int, int]] = []
    names = [normalize_name(d.content.name) for d in drafts]

    for i in range(len(drafts)):
        for j in range(i + 1, len(drafts)):
            same_name = names[i] == names[j]
            similar = _containment_ratio(names[i], names[j]) >= SIMILARITY_THRESHOLD
            shared_code = bool(set(drafts[i].standard_codes) & set(drafts[j].standard_codes))
            if same_name or similar or shared_code:
                pairs.append((i, j))

    return pairs


class SameTopicVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    same: bool
    reason: str = ""


class SameTopicJudge(Protocol):
    def __call__(self, a: TopicDraft, b: TopicDraft) -> SameTopicVerdict: ...


class DedupeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kept: list[TopicDraft]
    merges: list[Merge]
    drops: list[DropRecord]


_SAME_TOPIC_SYSTEM = (
    "你是小学课标知识依赖图的数据质检员。"
    "会给你两个候选知识点的名称与描述，判断它们讲的是不是**同一个知识点**。\n"
    "判 same=true：两者教的是同一件事，只是措辞、详略或来源条目不同。\n"
    "判 same=false：两者教的技能或概念不同，即便名称相近。\n"
    "拿不准时判 false —— 错误合并会永久丢失一个知识点，错误保留只是多一个节点。\n"
    "reason 用一句中文说明依据。"
)

_SAME_TOPIC_TOOL = {
    "name": SAME_TOPIC_TOOL_NAME,
    "description": "记录两个候选知识点是否为同一个",
    "input_schema": SameTopicVerdict.model_json_schema(),
}


class DeepSeekSameTopicJudge:
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic  # 懒加载：注入 client 的测试无需装 anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model

    def __call__(self, a: TopicDraft, b: TopicDraft) -> SameTopicVerdict:
        prompt = (
            f"知识点一\n名称：{a.content.name}\n描述：{a.content.description}\n\n"
            f"知识点二\n名称：{b.content.name}\n描述：{b.content.description}"
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=0,
            system=_SAME_TOPIC_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[_SAME_TOPIC_TOOL],
            tool_choice={"type": "tool", "name": SAME_TOPIC_TOOL_NAME},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == SAME_TOPIC_TOOL_NAME:
                # 强制 tool_choice 只保证"调了工具"，不保证参数合法，仍要过一遍校验
                return SameTopicVerdict.model_validate(block.input)
        raise ValueError(f"模型未调用 {SAME_TOPIC_TOOL_NAME} 工具，返回：{response.content!r}")


def _better_base(a: TopicDraft, b: TopicDraft) -> tuple[TopicDraft, TopicDraft]:
    """选合并基底：evidence 多者胜；并列取 description 长者；再并列取 draft_id 小者。"""
    key_a = (len(a.content.evidence), len(a.content.description))
    key_b = (len(b.content.evidence), len(b.content.description))
    if key_a > key_b or (key_a == key_b and a.draft_id <= b.draft_id):
        return a, b
    return b, a


def _merge_into(base: TopicDraft, other: TopicDraft) -> TopicDraft:
    seen_statements = {m.statement for m in base.content.misconceptions}
    merged = base.model_copy(deep=True)
    merged.content.evidence = list(dict.fromkeys(base.content.evidence + other.content.evidence))
    merged.content.misconceptions += [
        m for m in other.content.misconceptions if m.statement not in seen_statements
    ]
    merged.standard_codes = list(dict.fromkeys(base.standard_codes + other.standard_codes))
    return merged


def dedupe(drafts: list[TopicDraft], judge: SameTopicJudge) -> DedupeResult:
    by_id = {d.draft_id: d.model_copy(deep=True) for d in drafts}
    order = [d.draft_id for d in drafts]
    merges: list[Merge] = []
    drops: list[DropRecord] = []
    dropped: set[str] = set()

    for i, j in candidate_pairs(drafts):
        left_id, right_id = order[i], order[j]
        if left_id in dropped or right_id in dropped:
            continue

        verdict = judge(by_id[left_id], by_id[right_id])
        if verdict.same:
            base, other = _better_base(by_id[left_id], by_id[right_id])
            by_id[base.draft_id] = _merge_into(base, other)
            dropped.add(other.draft_id)
            merges.append(
                Merge(
                    kept_draft_id=base.draft_id,
                    dropped_draft_id=other.draft_id,
                    reason=verdict.reason,
                )
            )
            continue

        # 判为不同，但名字撞了 —— 不许共存，先试年级限定词
        left, right = by_id[left_id], by_id[right_id]
        if normalize_name(left.content.name) != normalize_name(right.content.name):
            continue
        if left.content.grade_start != right.content.grade_start:
            for draft in (left, right):
                draft.content.name = f"{draft.content.name}（{draft.content.grade_start}年级）"
            continue
        # 加了年级还撞名 —— 自动区分不了，丢后者交给人看
        dropped.add(right_id)
        drops.append(
            DropRecord(
                stage="dedupe",
                ref=right_id,
                reason="SAME_NAME_DIFFERENT_TOPIC",
                detail=f"与 {left_id} 同名同年级但判为不同知识点：{verdict.reason}",
            )
        )

    kept = [by_id[i] for i in order if i not in dropped]
    return DedupeResult(kept=kept, merges=merges, drops=drops)
