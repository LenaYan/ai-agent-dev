"""交叉审核：多个判定器各投一票，全票通过才留。

**分歧即淘汰**，因为本轮不承诺内容专业正确 —— 宁可少产出也别放可疑的进去。
被淘汰的写进 dropped.json，那是最值得人工复核的清单。

⚠️ **已知短板**：默认双票是 deepseek-v4-flash + v4-pro，同族模型的误判
高度相关，投两次约等于投一次 —— **不能理解成"两个模型都同意所以可信"**，
它只是同一套偏见问了两遍。理想是 Anthropic + DeepSeek 跨训练谱系互投；
配上 ANTHROPIC_API_KEY 后把 judges 列表换掉即可（见 build_deepseek_deps 的
调用方，把其中一档换成 AnthropicJudge/AnthropicFidelityJudge 类的实现）。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import (
    DropRecord,
    ProposedEdge,
    ReviewOutcome,
    TopicDraft,
    Vote,
)
from cn_curriculum_graph.validators.consistency import Judge

DEFAULT_MODEL = "deepseek-v4-flash"
FIDELITY_TOOL_NAME = "record_fidelity"
EDGE_REVIEW_TOOL_NAME = "record_edge_review"


class _VotePayload(BaseModel):
    """判定器工具的 input_schema。reviewer 由代码填，不问模型 —— 模型不该自报家门。"""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str = ""


class FidelityJudge(Protocol):
    def __call__(self, draft: TopicDraft) -> Vote: ...


class EdgeJudge(Protocol):
    def __call__(self, target: TopicDraft, edge: ProposedEdge) -> Vote: ...


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kept: list[TopicDraft] = Field(default_factory=list)
    kept_edges: dict[str, list[ProposedEdge]] = Field(default_factory=dict)
    outcomes: list[ReviewOutcome] = Field(default_factory=list)
    drops: list[DropRecord] = Field(default_factory=list)


_FIDELITY_SYSTEM = (
    "你是知识图谱的数据质检员。"
    "会给你一个知识点的『描述』和它声称的『原文出处』，"
    "判断描述是不是确实出自这句原文。\n"
    "判 approved=false：描述引入了原文里没有的内容，或与原文说的不是一回事。\n"
    "判 approved=true：描述是对原文的忠实转写或合理概括。\n"
    "措辞不同、更具体、更书面都算忠实；凭空增加的知识点不算。\n"
    "reason 用一句中文说明依据。"
)

_EDGE_SYSTEM = (
    "你是小学课标知识依赖图的数据质检员。"
    "会给你一条先修依赖边：目标知识点、前置知识点，以及声称的理由。\n"
    "判 approved=true：这个前置关系成立，且理由说得通。\n"
    "判 approved=false：前置关系不成立，或理由与实际关系对不上。\n"
    "拿不准判 false —— 一条错边会静默地把学习路径导偏，代价远大于少一条边。\n"
    "reason 用一句中文说明依据。"
)


class _DeepSeekVoter:
    def __init__(self, tool_name: str, system: str, client: Any | None, model: str) -> None:
        if client is None:
            import anthropic  # 懒加载：注入 client 的测试无需装 anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model
        self._tool_name = tool_name
        self._system = system
        self._tool = {
            "name": tool_name,
            "description": "记录审核结论",
            "input_schema": _VotePayload.model_json_schema(),
        }

    def _vote(self, prompt: str) -> Vote:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=0,
            system=self._system,
            messages=[{"role": "user", "content": prompt}],
            tools=[self._tool],
            tool_choice={"type": "tool", "name": self._tool_name},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == self._tool_name:
                # 强制 tool_choice 只保证"调了工具"，不保证参数合法，仍要过一遍校验
                payload = _VotePayload.model_validate(block.input)
                # reviewer 由代码填 —— 模型不该自报家门
                return Vote(
                    reviewer=self._model, approved=payload.approved, reason=payload.reason
                )
        raise ValueError(f"模型未调用 {self._tool_name} 工具，返回：{response.content!r}")


class DeepSeekFidelityJudge(_DeepSeekVoter):
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        super().__init__(FIDELITY_TOOL_NAME, _FIDELITY_SYSTEM, client, model)

    def __call__(self, draft: TopicDraft) -> Vote:
        return self._vote(
            f"原文出处：{draft.content.source_span}\n\n描述：{draft.content.description}"
        )


class DeepSeekEdgeJudge(_DeepSeekVoter):
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        super().__init__(EDGE_REVIEW_TOOL_NAME, _EDGE_SYSTEM, client, model)

    def __call__(self, target: TopicDraft, edge: ProposedEdge) -> Vote:
        return self._vote(
            f"目标知识点：{target.content.name} —— {target.content.description}\n"
            f"前置知识点 id：{edge.prerequisite_draft_id}\n"
            f"强度：{edge.strength}\n"
            f"声称的理由：{edge.reason}"
        )


def review_drafts(
    drafts: list[TopicDraft],
    fidelity_judges: list[FidelityJudge],
    name_judges: list[Judge],
) -> ReviewResult:
    """逐 draft 过两个维度：fidelity（全票通过才留）、name_desc（复用三档 judge）。

    name_desc 只有 topic_mismatch 才淘汰；scope_mismatch 是 WARNING 级，
    保留但记进 outcomes——与 validators.consistency 的两级严重性一一对应，
    不能简化成二值。

    **空 judges 列表当场炸**：判定用的是 all(v.approved for v in votes)，
    而 Python 里 all([]) == True —— 零个判定器会被读成满票通过，与"分歧即
    淘汰"的设计哲学正好相反。这通常是配置错误（例如构造 PipelineDeps 时
    忘了传 judges，相关字段又有 default_factory=list，静默就是空列表），
    不是合法输入，必须 raise 而不是静默放行或警告。
    """
    if not fidelity_judges:
        raise ValueError(
            "fidelity_judges 为空：零个判定器会被 all([]) 读成满票通过，"
            "这是配置错误（比如忘了传 judges），不是合法输入。"
        )
    if not name_judges:
        raise ValueError(
            "name_judges 为空：零个判定器会被 all([]) 读成满票通过，"
            "这是配置错误（比如忘了传 judges），不是合法输入。"
        )

    kept: list[TopicDraft] = []
    outcomes: list[ReviewOutcome] = []
    drops: list[DropRecord] = []

    for draft in drafts:
        rejected: list[str] = []

        fidelity_votes = [judge(draft) for judge in fidelity_judges]
        fidelity_ok = all(v.approved for v in fidelity_votes)
        outcomes.append(
            ReviewOutcome(
                target=draft.draft_id,
                aspect="fidelity",
                votes=fidelity_votes,
                approved=fidelity_ok,
            )
        )
        if not fidelity_ok:
            rejected.append("fidelity")

        # 名实一致复用三档 judge：topic_mismatch 才淘汰，
        # scope_mismatch 是 WARNING 级，保留但留痕 —— 与校验层的两级严重性一致
        name_votes: list[Vote] = []
        for judge in name_judges:
            verdict = judge(name=draft.content.name, description=draft.content.description)
            name_votes.append(
                Vote(
                    reviewer="name_desc",
                    approved=verdict.judgment != "topic_mismatch",
                    reason=f"{verdict.judgment}: {verdict.reason}",
                )
            )
        name_ok = all(v.approved for v in name_votes)
        outcomes.append(
            ReviewOutcome(
                target=draft.draft_id, aspect="name_desc", votes=name_votes, approved=name_ok
            )
        )
        if not name_ok:
            rejected.append("name_desc")

        if rejected:
            drops.append(
                DropRecord(
                    stage="review",
                    ref=draft.draft_id,
                    reason="REVIEW_REJECTED",
                    detail=f"未通过的维度：{','.join(rejected)}",
                )
            )
            continue
        kept.append(draft)

    return ReviewResult(kept=kept, outcomes=outcomes, drops=drops)


def review_edges(
    drafts_by_id: dict[str, TopicDraft],
    edges: dict[str, list[ProposedEdge]],
    edge_judges: list[EdgeJudge],
) -> ReviewResult:
    """逐边过 edge_reason 维度：全票通过才留。

    **防 KeyError**：`drafts_by_id` 与 `edges` 的键不保证同步 —— 调用方常见的
    用法是 `drafts_by_id` 只装 review_drafts 幸存下来的 draft，而 `edges` 若未经
    过滤，可能还留着已被淘汰目标的边（Task 4 的教训：预先算好的索引/映射在
    另一份状态被改写后可能失效）。查不到目标时不能让整层崩掉，也不能静默
    丢弃 —— 记一条带原因码的 DropRecord，跳过该目标下的全部边。

    **空 judges 列表当场炸**：同 review_drafts，all([]) == True 会把零个
    判定器读成满票通过，这是配置错误，不是合法输入。
    """
    if not edge_judges:
        raise ValueError(
            "edge_judges 为空：零个判定器会被 all([]) 读成满票通过，"
            "这是配置错误（比如忘了传 judges），不是合法输入。"
        )

    kept_edges: dict[str, list[ProposedEdge]] = {}
    outcomes: list[ReviewOutcome] = []
    drops: list[DropRecord] = []

    for target_id, proposed in edges.items():
        target = drafts_by_id.get(target_id)
        if target is None:
            drops.append(
                DropRecord(
                    stage="review",
                    ref=target_id,
                    reason="UNKNOWN_REVIEW_TARGET",
                    detail=(
                        f"目标 draft {target_id} 不在 drafts_by_id 中"
                        f"（可能已被上游淘汰），跳过其 {len(proposed)} 条候选边"
                    ),
                )
            )
            continue

        kept_edges[target_id] = []
        for edge in proposed:
            votes = [judge(target, edge) for judge in edge_judges]
            approved = all(v.approved for v in votes)
            outcomes.append(
                ReviewOutcome(
                    target=f"{target_id}<-{edge.prerequisite_draft_id}",
                    aspect="edge_reason",
                    votes=votes,
                    approved=approved,
                )
            )
            if approved:
                kept_edges[target_id].append(edge)
            else:
                drops.append(
                    DropRecord(
                        stage="review",
                        ref=target_id,
                        reason="REVIEW_REJECTED",
                        detail=f"边 {target_id}<-{edge.prerequisite_draft_id} 未通过审核",
                    )
                )

    return ReviewResult(kept_edges=kept_edges, outcomes=outcomes, drops=drops)
