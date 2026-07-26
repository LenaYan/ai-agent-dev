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
        raise ValueError(f"模型未调用 {EXTRACT_TOOL_NAME} 工具，返回：{response.content!r}")


def extract_all(
    chunks: list[Chunk], extractor: Extractor
) -> tuple[list[TopicDraft], list[DropRecord]]:
    """逐 chunk 抽取。单个 chunk 失败不中断整批 —— 记账后继续。"""
    drafts: list[TopicDraft] = []
    drops: list[DropRecord] = []

    for chunk in chunks:
        try:
            batch = extractor(chunk)
        except PROGRAMMING_ERRORS:  # 程序 bug，不该伪装成 API 失败，直接冒泡
            raise
        except Exception as exc:  # noqa: BLE001 —— 任何失败都只影响这一条
            drops.append(
                DropRecord(
                    stage="extract",
                    ref=chunk.id,
                    reason="EXTRACT_FAILED",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        if not batch.drafts:
            drops.append(
                DropRecord(
                    stage="extract", ref=chunk.id, reason="NO_DRAFTS", detail=chunk.text[:60]
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
