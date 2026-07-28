"""从课标条目抽取候选知识点。

结构化输出走**强制工具调用**而非原生 output_format：DeepSeek 的兼容端点
照收 output_format 却不遵守（实测返回自由文本），强制工具调用才可移植。
工具的 input_schema 就是 DraftBatch —— 也就是说，模型能填什么字段，
由类型系统而非提示词约束。

**为什么关 thinking。** DeepSeek v4 默认开思考模式，而思考模式下不接受强制
`tool_choice`（实测直接返回 400："Thinking mode does not support this
tool_choice"）。名实抽取是有界任务（拆条目、填字段），也不需要思考预算。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from cn_curriculum_graph.errors import ToolCallMissingError
from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import (
    PROGRAMMING_ERRORS,
    Chunk,
    DraftBatch,
    DropRecord,
    TopicDraft,
)

DEFAULT_MODEL = "deepseek-v4-flash"
EXTRACT_TOOL_NAME = "record_topics"

_SYSTEM = (
    "你是小学课标知识依赖图的构建者。"
    "会给你一条课程标准条目的正文，请把它拆成可教、可评的知识点（micro-topic）。\n"
    "一条条目可能对应一个知识点，也可能对应多个；如果拆不出任何可教的知识点，返回空列表。\n"
    "每个知识点要求：\n"
    "- name：简短的知识点名称，必须能概括 description 的主要内容\n"
    "- description：这个知识点具体教什么，一到两句\n"
    "- evidence：至少一条可观察可验证的掌握判据，写成能直接拿去考查的样子\n"
    "- assessment_prompt：一句面向家长或老师的口头提问\n"
    "- misconceptions：孩子典型的想错方式，没有把握就留空，不要编\n"
    "- source_span：**原文中支撑这个知识点的那一句**，必须逐字来自给你的正文\n"
    "- grade_start/grade_end：中国义务教育年级，1-9\n"
    "只依据给你的正文，不要引入正文之外的内容。"
)

_EXTRACT_TOOL = {
    "name": EXTRACT_TOOL_NAME,
    "description": "记录从这条课标条目抽出的全部知识点",
    "input_schema": DraftBatch.model_json_schema(),
}


class Extractor(Protocol):
    def __call__(self, chunk: Chunk) -> DraftBatch: ...


class DeepSeekExtractor:
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic  # 懒加载：注入 client 的测试无需装 anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model

    def __call__(self, chunk: Chunk) -> DraftBatch:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            temperature=0,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"课标条目正文：\n{chunk.text}"}],
            tools=[_EXTRACT_TOOL],
            tool_choice={"type": "tool", "name": EXTRACT_TOOL_NAME},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == EXTRACT_TOOL_NAME:
                # 强制 tool_choice 只保证"调了工具"，不保证参数合法
                return DraftBatch.model_validate(block.input)
        raise ToolCallMissingError(f"模型未调用 {EXTRACT_TOOL_NAME} 工具，返回：{response.content!r}")


# 空返回的重试次数（总尝试次数 = 1 + 这个数）。
#
# **为什么要重试**：2026-07-28 实测（docs/pipeline-reproducibility.md）——
# 同一份课标原文跑三次，28 个 chunk 里 **8 个（29%）至少一次颗粒无收**，
# 而且每次是不同的几块。整组知识点连同误概念一起消失，是跨运行不可复现的
# 最大单项来源，也是评测标签活不过一次重跑的主因之一。
#
# 逐 chunk 复测：同一 chunk 连发 5 次，空返回率约 20~40%，而且
# **flash 与 pro 没有差别**（pro 在 #004 上还更高：2/5 vs 1/5）。所以这不是
# 模型能力问题，换模型解决不了 —— 是 `_SYSTEM` 里"如果拆不出任何可教的
# 知识点，返回空列表"这个逃生舱被误用在明显可教的内容上
# （#004 的原文是"探索加法和减法的算理与算法，会整数加减法"）。
#
# **为什么这不是"逼模型硬凑"**：素材里 28 条全部是实打实的课标条目，
# 逐条读过，没有一条拆不出可教的知识点。所以空返回一律是误判的逃生舱。
# 但仍然设上限、且全空时照常记 `NO_DRAFTS` —— 万一将来素材里真有空条目
# （比如章节标题被误切成 chunk），它还是会被如实记下来，而不是被重试
# 逼出一个编造的知识点。记账里写明试了几次，好让"重试过仍然空"与
# "一次就空"在 dropped.json 里可区分。
#
# **重试发的是逐字相同的请求，这样是有效的 —— 已验证，不要再怀疑一遍。**
# 加完重试的第一轮里 #004 连空 3 次，当时怀疑是服务端对相同请求做了缓存
# （若成立，加重试次数就是无效的，得给请求带扰动）。**实测证伪**：对 #004
# 跑 8 轮重试，原样重试的基础空返回率 20%（2/10），而 `P(空 | 上一次空)`
# 是 **0/2**；带扰动的对照组是 0/1。若真被缓存粘住，这两个条件概率应该
# 接近 100%。（条件样本只有 2 与 1 个，证据强度有限，但方向明确反对粘滞。）
#
# 那次"连空 3 次"也不需要别的解释：基础率 20% 下单个 chunk 连空三次是
# 0.8%，而 **28 个 chunk 里至少出现一个的概率是 1-0.992^28 ≈ 20%** ——
# 一轮里撞上一个是正常的，不是异常。这也是为什么上限定在 2 就够：
# 期望每轮残留不到 1 个 chunk。
EMPTY_RETRIES = 2


def extract_all(
    chunks: list[Chunk], extractor: Extractor
) -> tuple[list[TopicDraft], list[DropRecord]]:
    """逐 chunk 抽取。单个 chunk 失败不中断整批 —— 记账后继续。

    空返回会重试（见 `EMPTY_RETRIES`）；抛异常不重试 —— 那是另一类失败，
    重试策略该由调用方/编排层决定，这里只负责不让它中断整批。
    """
    drafts: list[TopicDraft] = []
    drops: list[DropRecord] = []

    for chunk in chunks:
        batch = None
        attempts = 0
        failure: Exception | None = None
        while attempts <= EMPTY_RETRIES:
            attempts += 1
            try:
                batch = extractor(chunk)
            except PROGRAMMING_ERRORS:  # 程序 bug，不该伪装成 API 失败，直接冒泡
                raise
            except Exception as exc:  # noqa: BLE001 —— 任何失败都只影响这一条
                failure = exc
                break
            if batch.drafts:
                break

        if failure is not None:
            drops.append(
                DropRecord(
                    stage="extract",
                    ref=chunk.id,
                    reason="EXTRACT_FAILED",
                    detail=f"{type(failure).__name__}: {failure}",
                )
            )
            continue

        if batch is None or not batch.drafts:
            drops.append(
                DropRecord(
                    stage="extract",
                    ref=chunk.id,
                    reason="NO_DRAFTS",
                    detail=f"试了 {attempts} 次都是空返回｜{chunk.text[:60]}",
                )
            )
            continue

        for index, content in enumerate(batch.drafts):
            # DraftContent 只有 grade_start/grade_end 各自的 ge/le 边界，没有
            # 顺序校验（那条顺序校验在对外 schema 的 Topic._grade_range_ordered
            # 里，见 cn_curriculum_graph/models.py:114-120）——刻意不在这里给
            # DraftContent 加 pydantic validator：那会让整个 DraftBatch.model_validate
            # 失败，把同一 chunk 里其他合格的 draft 一起拖下水，与"单条失败不
            # 中断整批"冲突。区间填反是 LLM 填数值区间最常见的错法之一，
            # 必须单独拦下、单独丢弃，不能一路穿到 assemble 才崩掉整条流水线。
            if content.grade_end < content.grade_start:
                drops.append(
                    DropRecord(
                        stage="extract",
                        ref=f"{chunk.id}-{index}",
                        reason="GRADE_RANGE_INVERTED",
                        detail=(
                            f"{content.name}：grade_start={content.grade_start} > "
                            f"grade_end={content.grade_end}"
                        ),
                    )
                )
                continue
            drafts.append(
                TopicDraft(
                    # index 补零到两位：chunk.id 本身补零到三位
                    # （f"{stem}#{ordinal:03d}"），若 index 是裸整数，
                    # 一条条目抽出 ≥10 个知识点时字典序会与抽取序反相关
                    # （"...-10" < "...-2"）。edges.py 的
                    # candidate_prerequisites 靠"同年级节点按 draft_id
                    # 字典序定先后方向"断双向边防 CYCLE，这条规则的前提
                    # 正是 draft_id 字典序 ≈ 抽取序，补零是维持这个前提的必要条件。
                    draft_id=f"{chunk.id}-{index:02d}",
                    chunk_id=chunk.id,
                    standard_codes=[chunk.standard_code],
                    content=content,
                )
            )

    return drafts, drops
