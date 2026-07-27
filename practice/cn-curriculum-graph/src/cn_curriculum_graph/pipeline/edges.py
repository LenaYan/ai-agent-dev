"""生成先修依赖边。

朴素做法是两两配对，30 个节点就是 435 次调用。这里改成
**剪枝 + 每节点一次**：把该节点和它的全部候选前置一起给模型，
让它一次输出选中的边。调用次数从 N² 降到 N。

剪枝规则来自校验规则的反推，见 docs/pipeline-design.md §3.4。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.errors import ToolCallMissingError
from cn_curriculum_graph.pipeline.models import (
    PROGRAMMING_ERRORS,
    DropRecord,
    ProposedEdge,
    ProposedEdgeBatch,
    TopicDraft,
)

DEFAULT_MODEL = "deepseek-v4-flash"
EDGE_TOOL_NAME = "record_prerequisites"
# 跨度超过两个年级的先修基本是间接的，砍掉让传递性去表达
MAX_GRADE_GAP = 2


def candidate_prerequisites(drafts: list[TopicDraft]) -> dict[str, list[TopicDraft]]:
    """候选前置：年级不晚于目标，且跨度不超过 MAX_GRADE_GAP。

    **同年级只保留单向**（按 draft_id 字典序）。理由：同年级互为候选时，
    模型会对两个节点各点一次头，产出 A→B 且 B→A 的双向边，直接触发校验层的
    CYCLE（ERROR），整批产出被自己的 CI 拒掉。首次真实运行已实测到这一幕
    ——当时是 edge judge 把两条都否了才侥幸没成环，靠判定器兜住而不是靠剪枝挡住。

    按 draft_id 定方向是**取舍不是定论**：简单、确定、零额外调用，
    但方向可能与真实先修关系相反。替代方案是让模型选方向（多一次调用）。
    """
    candidates: dict[str, list[TopicDraft]] = {}
    for target in drafts:
        candidates[target.draft_id] = [
            other
            for other in drafts
            if other.draft_id != target.draft_id
            and other.content.grade_start <= target.content.grade_start
            and target.content.grade_start - other.content.grade_start <= MAX_GRADE_GAP
            # 同年级：只有 draft_id 在前的能当前置，断掉反向候选
            and not (
                other.content.grade_start == target.content.grade_start
                and other.draft_id > target.draft_id
            )
        ]
    return candidates


_SYSTEM = (
    "你是小学课标知识依赖图的构建者。"
    "会给你一个『目标知识点』和一组『候选前置知识点』，"
    "请挑出其中真正是目标知识点先修条件的那些。\n"
    "先修的判据：不先掌握候选，就学不动目标（hard）；"
    "或有助于理解但非必需（soft）。\n"
    "只能从给定候选里挑，必须原样引用它们的 id。挑不出就返回空列表 —— "
    "少一条边远好过一条错边，错边会静默地把学习路径导偏。\n"
    "reason 写一句中文，说明为什么它是前置，这句话会被直接拿去给学习者讲解。"
)

_TOOL = {
    "name": EDGE_TOOL_NAME,
    "description": "记录目标知识点的先修依赖",
    "input_schema": ProposedEdgeBatch.model_json_schema(),
}


class EdgeProposer(Protocol):
    def __call__(
        self, target: TopicDraft, candidates: list[TopicDraft]
    ) -> ProposedEdgeBatch: ...


class DeepSeekEdgeProposer:
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic  # 懒加载：注入 client 的测试无需装 anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model

    def __call__(self, target: TopicDraft, candidates: list[TopicDraft]) -> ProposedEdgeBatch:
        listed = "\n".join(
            f"- id={c.draft_id}｜{c.content.name}（{c.content.grade_start}年级）："
            f"{c.content.description}"
            for c in candidates
        )
        prompt = (
            f"目标知识点\n"
            f"名称：{target.content.name}（{target.content.grade_start}年级）\n"
            f"描述：{target.content.description}\n\n"
            f"候选前置知识点\n{listed}"
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            temperature=0,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": EDGE_TOOL_NAME},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == EDGE_TOOL_NAME:
                # 强制 tool_choice 只保证"调了工具"，不保证参数合法，仍要过一遍校验
                return ProposedEdgeBatch.model_validate(block.input)
        raise ToolCallMissingError(f"模型未调用 {EDGE_TOOL_NAME} 工具，返回：{response.content!r}")


def propose_all(
    drafts: list[TopicDraft], proposer: EdgeProposer
) -> tuple[dict[str, list[ProposedEdge]], list[DropRecord]]:
    """逐 draft 提议前置边。单个 target 失败不中断整批 —— 记账后继续。

    **候选池校验，而非"全体草稿"校验。** `known` 只能拦住彻底编造的 id；
    一个真实存在的 draft_id 仍可能是自环（target 引用自己），或是一个
    确实存在、但在剪枝阶段就因年级倒挂/跨度过大被排除出候选池的 id ——
    这两种都不在 `known` 检查的射程内，却恰恰是剪枝本该防住的东西。
    所以这里额外核对模型引用的 id 是否落在**当次实际发给它的候选池**里，
    而不是只核对"是不是某个真实存在的 draft"。
    """
    known = {d.draft_id for d in drafts}
    candidates = candidate_prerequisites(drafts)
    edges: dict[str, list[ProposedEdge]] = {d.draft_id: [] for d in drafts}
    drops: list[DropRecord] = []

    for target in drafts:
        pool = candidates[target.draft_id]
        if not pool:
            continue

        try:
            batch = proposer(target, pool)
        except PROGRAMMING_ERRORS:  # 程序 bug，不该伪装成 API 失败，直接冒泡
            raise
        except Exception as exc:  # noqa: BLE001 —— 单个目标失败不影响其余
            drops.append(
                DropRecord(
                    stage="edges",
                    ref=target.draft_id,
                    reason="EDGES_FAILED",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        pool_ids = {c.draft_id for c in pool}
        for edge in batch.edges:
            prereq_id = edge.prerequisite_draft_id
            if prereq_id not in known:
                drops.append(
                    DropRecord(
                        stage="edges",
                        ref=target.draft_id,
                        reason="UNKNOWN_PREREQUISITE",
                        detail=f"模型引用了不存在的 id：{prereq_id}",
                    )
                )
                continue
            if prereq_id not in pool_ids:
                drops.append(
                    DropRecord(
                        stage="edges",
                        ref=target.draft_id,
                        reason="NON_CANDIDATE_PREREQUISITE",
                        detail=(
                            f"模型引用了真实存在但不在候选池内的 id：{prereq_id}"
                            "（自环或已被剪枝规则排除，放行会绕过 GRADE_INVERSION 等约束）"
                        ),
                    )
                )
                continue
            edges[target.draft_id].append(edge)

    return edges, drops
