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
from typing import Any, Literal, Protocol

from pydantic import BaseModel, ConfigDict, Field

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.errors import ToolCallMissingError
from cn_curriculum_graph.pipeline.models import (
    PROGRAMMING_ERRORS,
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


# 三档而非 true/false —— 与 validators.consistency.Judgment 同源的设计决定，
# 这里是它的**第二次实证**。
#
# 2026-07-27，28 条真实课标语料全量跑完：72 个 draft 里 45 个被 fidelity 淘汰
# （62%），而这 45 条里 **21 条（47%）是分歧淘汰**，两个模型看法相反。分歧的
# 形状是关键：
#
#   原文「理解数位的含义」→ 描述「理解个位、十位…的含义」
#       flash ✅「合理展开」   pro ❌「凭空增加」
#   原文「会比较万以内数的大小」→ 描述「…掌握从高位到低位逐位比较」
#       flash ❌「额外增加」   pro ✅「合理具体化」
#
# **后一条的方向是反的。** 不是两个模型各有稳定立场，而是"合理展开算不算忠实"
# 在二值框架下没有稳定答案 —— 模型每次随机塞进一个格子。
#
# 更根本的矛盾：课标条目是纲领性文字（"理解数位的含义"），知识依赖图的节点却
# 必须是可教、可测的具体知识点。要求 description 严格忠于 source_span，等于
# 要求节点停留在纲领的抽象层级 —— 那样的节点没法用。**忠实性与有用性在课标
# 这类文本上直接冲突**，二值判定表达不了这个冲突，三档能。
#
# 用 Literal 而非 Enum：pydantic 会内联成 {"enum": [...]}，不生成 $defs 引用
# —— 各家结构化输出/工具 schema 对 $ref 的支持参差不齐（见 memory 的三档判定那条）。
FidelityJudgment = Literal["faithful", "reasonable_elaboration", "fabricated"]


class _FidelityPayload(BaseModel):
    """**给模型的 input_schema。字段顺序是有意的：`reason` 在 `judgment` 之前。**

    工具调用的参数是顺序生成的，把判定放在理由前面等于让模型先投票再找理由。
    旧的 `_VotePayload` 正是 `approved` 在前、`reason` 在后。这里反过来，
    相当于结构化输出里的 CoT：先写依据，再落判定。

    诚实说明：这一条是**设计改进，不是上面那组分歧统计支持的结论** ——
    那组数据证明的是档位不够，不是字段顺序有害。两件事一起改，效果分别测。
    """

    model_config = ConfigDict(extra="forbid")

    reason: str = ""
    judgment: FidelityJudgment


class FidelityVerdict(_FidelityPayload):
    """判定器的返回值 = 模型产出的两个字段 + 代码填的 `reviewer`。

    `reviewer` 刻意**不在** `_FidelityPayload` 里 —— 模型不该自报家门（与
    `_VotePayload` 的注释同源）。但它必须被保留下来：2026-07-27 那次分歧分析
    （flash ✅ 合理展开 / pro ❌ 凭空增加）之所以做得出来，正是因为 outcomes
    里记着是哪个模型投的哪一票。把它并成 `reviewer="fidelity"` 会让同类分析
    在下一次直接失明。
    """

    reviewer: str = ""

    @property
    def is_faithful(self) -> bool:
        """三档 → 两级后果：只有 `fabricated` 淘汰。

        `reasonable_elaboration` 保留但留痕 —— 与 `scope_mismatch` 判 WARNING
        而非 ERROR 是同一个取舍：真问题要拦住，灰区要留痕而不是拦住。
        """
        return self.judgment != "fabricated"


class FidelityJudge(Protocol):
    def __call__(self, draft: TopicDraft) -> FidelityVerdict: ...


class EdgeJudge(Protocol):
    def __call__(self, target: TopicDraft, edge: ProposedEdge) -> Vote: ...


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kept: list[TopicDraft] = Field(default_factory=list)
    kept_edges: dict[str, list[ProposedEdge]] = Field(default_factory=dict)
    outcomes: list[ReviewOutcome] = Field(default_factory=list)
    drops: list[DropRecord] = Field(default_factory=list)


_FIDELITY_SYSTEM = (
    "你是课标知识图谱的数据质检员。"
    "会给你一个知识点的『描述』和它声称的『原文出处』（一句课标条目）。\n"
    "\n"
    "**先在 reason 里写清依据，再给出 judgment。**\n"
    "\n"
    "课标条目是纲领性文字，往往只有一句话（如「理解数位的含义」）；"
    "而知识点的描述需要具体到可教、可测。所以「比原文具体」是常态，不是问题。"
    "你要区分的是「把原文讲的那件事说具体了」和「换了一件事说」。\n"
    "\n"
    "judgment 三选一：\n"
    "- faithful：描述与原文说的是同一件事，只是措辞不同或更书面。\n"
    "- reasonable_elaboration：描述把原文那件事**具体化**了 —— 列举了原文所指的"
    "对象（如「数位」→「个位、十位、百位」）、点明了原文所指的方法（如「会比较"
    "大小」→「从高位到低位逐位比较」）、或补上了原文动词的自然延伸（如「了解"
    "符号的含义」→「能用符号表示大小关系」）。**这一档是合格的，不是问题。**\n"
    "- fabricated：描述引入了原文之外的知识点，或与原文说的不是同一件事。"
    "例如原文只说「知道用算盘可以表示多位数」，描述却讲起算盘的框、梁、档、"
    "上珠、下珠 —— 那是另一个知识点，不是这句话的具体化。\n"
    "\n"
    "拿不准是 reasonable_elaboration 还是 fabricated 时，问自己：**去掉描述里"
    "多出来的部分，剩下的还是原文那件事吗？** 是 → reasonable_elaboration；"
    "多出来的部分本身就是一个独立知识点 → fabricated。\n"
    # 试过在这里加一条「多出来的部分若够格当另一条课标条目 → fabricated」，
    # 想压掉『大小比较』『用计算器』那两条漏报。**实测更差**：漏报从 2 涨到 3
    # （「了解奇数、偶数、质数和合数」→「能判断奇数偶数」这条内容缺失的样本
    # 反而被放行了）。推测是把注意力全引向「多写」，挤掉了「少写」。
    # 保留这条失败记录而不是删掉：它说明 fidelity 其实有两个正交维度 ——
    # **多写（编造）和少写（缺失）** —— 而现在这三档只覆盖了「多写」。
    # 见 docs/mcp-server-design.md 的遗留问题一节。
    "拿不准是 faithful 还是 reasonable_elaboration 时，倾向 reasonable_elaboration"
    "（两者后果相同，都保留）。"
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
    def __init__(
        self,
        tool_name: str,
        system: str,
        client: Any | None,
        model: str,
        payload_model: type[BaseModel] = _VotePayload,
    ) -> None:
        if client is None:
            import anthropic  # 懒加载：注入 client 的测试无需装 anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model
        self._tool_name = tool_name
        self._system = system
        self._payload_model = payload_model
        self._tool = {
            "name": tool_name,
            "description": "记录审核结论",
            "input_schema": payload_model.model_json_schema(),
        }

    def _call(self, prompt: str) -> BaseModel:
        """发一次强制工具调用，返回校验过的 payload。

        两类判定器（二值的边审核、三档的 fidelity）共用这段网络与工具调用
        管线，只有 payload 类型不同 —— 复制两份是"改一处漏一处"的温床。
        """
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
                return self._payload_model.model_validate(block.input)
        raise ToolCallMissingError(f"模型未调用 {self._tool_name} 工具，返回：{response.content!r}")


    def _vote(self, prompt: str) -> Vote:
        payload = self._call(prompt)
        # reviewer 由代码填 —— 模型不该自报家门
        return Vote(reviewer=self._model, approved=payload.approved, reason=payload.reason)


class DeepSeekFidelityJudge(_DeepSeekVoter):
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        super().__init__(
            FIDELITY_TOOL_NAME, _FIDELITY_SYSTEM, client, model, payload_model=_FidelityPayload
        )

    def __call__(self, draft: TopicDraft) -> FidelityVerdict:
        payload = self._call(
            f"原文出处：{draft.content.source_span}\n\n描述：{draft.content.description}"
        )
        return FidelityVerdict(
            reason=payload.reason, judgment=payload.judgment, reviewer=self._model
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

        # review 是全流水线调用量最高的一层（约为 extract 的 4-6 倍），任意一次
        # 限流/超时/网络抖动都会撞上。判定器裸调会让单次失败直接掀翻整批
        # run_pipeline（设计文档 §6：单条目失败不中断整批）。判定器没能表态时，
        # 按本层"分歧即淘汰、宁可少产出"的精神保守处理——该草稿判为淘汰，
        # 而不是放行；原因码与普通的 REVIEW_REJECTED 区分开，便于区分
        # "评审说不行" 和 "评审根本没跑起来"。
        try:
            # 三档 → 两级：faithful / reasonable_elaboration 都算通过，只有
            # fabricated 淘汰。档位本身写进 Vote.judgment 留痕（见 Vote 文档）——
            # "这个描述比原文具体"必须能被程序读出来，否则等于没留。
            fidelity_votes = [
                Vote(
                    reviewer=v.reviewer or "fidelity",
                    approved=v.is_faithful,
                    reason=v.reason,
                    judgment=v.judgment,
                )
                for v in (judge(draft) for judge in fidelity_judges)
            ]
        except PROGRAMMING_ERRORS:  # 程序 bug，不该伪装成判定器失败，直接冒泡
            raise
        except Exception as exc:  # noqa: BLE001 —— 单条目失败不中断整批
            drops.append(
                DropRecord(
                    stage="review",
                    ref=draft.draft_id,
                    reason="FIDELITY_JUDGE_FAILED",
                    detail=f"{type(exc).__name__}: {exc}（判定器未能表态，保守淘汰该草稿）",
                )
            )
            continue

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
        try:
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
        except PROGRAMMING_ERRORS:
            raise
        except Exception as exc:  # noqa: BLE001 —— 单条目失败不中断整批
            drops.append(
                DropRecord(
                    stage="review",
                    ref=draft.draft_id,
                    reason="NAME_JUDGE_FAILED",
                    detail=f"{type(exc).__name__}: {exc}（判定器未能表态，保守淘汰该草稿）",
                )
            )
            continue

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
            pair_ref = f"{target_id}<-{edge.prerequisite_draft_id}"
            try:
                votes = [judge(target, edge) for judge in edge_judges]
            except PROGRAMMING_ERRORS:  # 程序 bug，不该伪装成判定器失败，直接冒泡
                raise
            except Exception as exc:  # noqa: BLE001 —— 单条边失败不中断整批
                drops.append(
                    DropRecord(
                        stage="review",
                        ref=pair_ref,
                        reason="EDGE_JUDGE_FAILED",
                        detail=f"{type(exc).__name__}: {exc}（判定器未能表态，保守淘汰该边）",
                    )
                )
                continue

            approved = all(v.approved for v in votes)
            outcomes.append(
                ReviewOutcome(
                    target=pair_ref,
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
                        # ref 用 "target<-prereq" 而非裸 target_id：与 review_drafts
                        # 淘汰节点的 REVIEW_REJECTED（ref=draft_id）区分开，否则
                        # 同一 reason 码在两处含义不同，下游按 ref 归因会分不清
                        # 到底丢的是一整个节点还是一条边。
                        ref=pair_ref,
                        reason="REVIEW_REJECTED",
                        detail=f"边 {pair_ref} 未通过审核",
                    )
                )

    return ReviewResult(kept_edges=kept_edges, outcomes=outcomes, drops=drops)


def filter_edges_by_kept_drafts(
    proposed: dict[str, list[ProposedEdge]], kept_ids: set[str]
) -> tuple[dict[str, list[ProposedEdge]], list[DropRecord]]:
    """审核淘汰节点后，剔除两端不再存活的边，**每条都留痕**。

    这段逻辑原先内联在 run_pipeline 里。抽出来是因为它是流水线语义而非编排
    机制：LangGraph 版必须行为完全一致，复制一份必然改一处漏一处。
    """
    surviving: dict[str, list[ProposedEdge]] = {}
    drops: list[DropRecord] = []

    for target, group in proposed.items():
        if target not in kept_ids:
            drops += [
                DropRecord(
                    stage="review",
                    ref=f"{target}<-{e.prerequisite_draft_id}",
                    reason="EDGE_TARGET_REJECTED",
                    detail=f"目标 draft {target} 被 review 淘汰，指向它的边一并丢弃",
                )
                for e in group
            ]
            continue

        kept: list[ProposedEdge] = []
        for e in group:
            if e.prerequisite_draft_id in kept_ids:
                kept.append(e)
            else:
                drops.append(
                    DropRecord(
                        stage="review",
                        ref=f"{target}<-{e.prerequisite_draft_id}",
                        reason="EDGE_PREREQUISITE_REJECTED",
                        detail=(
                            f"前置 draft {e.prerequisite_draft_id} 被 review 淘汰，"
                            f"边 {target}<-{e.prerequisite_draft_id} 丢弃"
                        ),
                    )
                )
        surviving[target] = kept

    return surviving, drops


def detect_orphans(
    kept_drafts: list[TopicDraft],
    proposed_before: dict[str, list[ProposedEdge]],
    kept_edges: dict[str, list[ProposedEdge]],
) -> list[DropRecord]:
    """找出"原本有前置、因本次淘汰而失去全部前置"的节点。

    只记账不丢弃：这些节点本身没问题，问题在于它们的前置没了。校验层的
    ISOLATED_TOPIC 只说"这个节点没有边"，说不出"它本来有、是被这次淘汰弄没的"
    —— 后者才是可行动的信息（该去看 dropped.json 里那个被淘汰的前置该不该淘汰）。
    """
    drops: list[DropRecord] = []
    for draft in kept_drafts:
        before = proposed_before.get(draft.draft_id, [])
        after = kept_edges.get(draft.draft_id, [])
        if before and not after:
            lost = ", ".join(e.prerequisite_draft_id for e in before)
            drops.append(
                DropRecord(
                    stage="review",
                    ref=draft.draft_id,
                    reason="ORPHANED_BY_REJECTION",
                    detail=f"原有前置 [{lost}] 全部被淘汰，该节点现已无前置",
                )
            )
    return drops
