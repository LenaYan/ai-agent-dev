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


_MAX_DEDUP_ROUNDS = 10


def _group_survivors_by_name(
    by_id: dict[str, TopicDraft], order: list[str], dropped: set[str]
) -> dict[str, list[str]]:
    """按当前（可能已带年级限定词的）归一化名给未丢弃的草稿分组。"""
    groups: dict[str, list[str]] = {}
    for draft_id in order:
        if draft_id in dropped:
            continue
        key = normalize_name(by_id[draft_id].content.name)
        groups.setdefault(key, []).append(draft_id)
    return groups


def _resolve_same_name_conflicts(
    by_id: dict[str, TopicDraft],
    order: list[str],
    dropped: set[str],
    drops: list[DropRecord],
) -> None:
    """阶段二：纯规则消歧，作用于阶段一幸存者。

    安全前提：归一化名相同的草稿必然会被 candidate_pairs 的第一条规则配成候选对，
    所以阶段一必定已对它们调用过 judge —— 走到这里的同名幸存者，一定是被判为
    "不同" 才没被合并掉的，规则消歧不会误伤应合并的。

    加了限定词的名字可能与第三方的原名再次撞上，所以要反复分组直到收敛；
    设轮数上限防止病态输入下死循环。
    """
    for _round in range(_MAX_DEDUP_ROUNDS):
        groups = _group_survivors_by_name(by_id, order, dropped)
        conflicts = [ids for ids in groups.values() if len(ids) > 1]
        if not conflicts:
            return

        for ids in conflicts:
            by_grade: dict[int, list[str]] = {}
            for draft_id in ids:
                by_grade.setdefault(by_id[draft_id].content.grade_start, []).append(draft_id)

            if len(by_grade) == len(ids):
                # 组内各成员年级互不相同 —— 都加年级限定词区分
                for draft_id in ids:
                    draft = by_id[draft_id]
                    draft.content.name = f"{draft.content.name}（{draft.content.grade_start}年级）"
            else:
                # 有同年级的 —— 同年级子组里留 draft_id 字典序最小者，其余丢弃记账；
                # 年级不撞车的成员本轮不动，下一轮重新分组时会落入上面的分支
                for grade_ids in by_grade.values():
                    if len(grade_ids) <= 1:
                        continue
                    grade_ids_sorted = sorted(grade_ids)
                    keep_id = grade_ids_sorted[0]
                    for drop_id in grade_ids_sorted[1:]:
                        dropped.add(drop_id)
                        drops.append(
                            DropRecord(
                                stage="dedupe",
                                ref=drop_id,
                                reason="SAME_NAME_DIFFERENT_TOPIC",
                                detail=f"与 {keep_id} 同名同年级但判为不同知识点",
                            )
                        )

    # 达到轮数上限仍未收敛 —— 绝不静默放过：保留字典序最小者，其余丢弃记账
    for ids in _group_survivors_by_name(by_id, order, dropped).values():
        if len(ids) <= 1:
            continue
        ids_sorted = sorted(ids)
        keep_id = ids_sorted[0]
        for drop_id in ids_sorted[1:]:
            dropped.add(drop_id)
            drops.append(
                DropRecord(
                    stage="dedupe",
                    ref=drop_id,
                    reason="SAME_NAME_DIFFERENT_TOPIC",
                    detail=f"同名消歧达到 {_MAX_DEDUP_ROUNDS} 轮上限仍与 {keep_id} 撞名，人工介入",
                )
            )


def dedupe(drafts: list[TopicDraft], judge: SameTopicJudge) -> DedupeResult:
    by_id = {d.draft_id: d.model_copy(deep=True) for d in drafts}
    order = [d.draft_id for d in drafts]
    merges: list[Merge] = []
    drops: list[DropRecord] = []
    dropped: set[str] = set()

    # 阶段一：只做 LLM 判定与合并。同名消歧不在这个循环里做 ——
    # candidate_pairs 是基于原始 drafts 预先算好的索引对，若在循环内就地
    # 改写名字/丢弃，处理到第三个及以后的候选对时读到的就是"部分改写过"的
    # 状态，会导致同名判断被绕过（三个以上同名草稿时最后一个带着裸名逃逸）。
    for i, j in candidate_pairs(drafts):
        left_id, right_id = order[i], order[j]
        if left_id in dropped or right_id in dropped:
            continue

        try:
            verdict = judge(by_id[left_id], by_id[right_id])
        except Exception as exc:  # noqa: BLE001 —— 单对失败不能中断整批
            drops.append(
                DropRecord(
                    stage="dedupe",
                    ref=left_id,
                    reason="SAME_TOPIC_JUDGE_FAILED",
                    detail=(
                        f"{type(exc).__name__}: {exc}"
                        f"（与 {right_id} 配对判定失败，两者均保留未合并）"
                    ),
                )
            )
            continue

        if not verdict.same:
            continue

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

    # 阶段二：纯规则消歧，只对阶段一的幸存者按当前名字重新分组处理
    _resolve_same_name_conflicts(by_id, order, dropped, drops)

    kept = [by_id[i] for i in order if i not in dropped]
    return DedupeResult(kept=kept, merges=merges, drops=drops)
