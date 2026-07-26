"""从课标条目抽取候选知识点。

结构化输出走**强制工具调用**而非原生 output_format：DeepSeek 的兼容端点
照收 output_format 却不遵守（实测返回自由文本），强制工具调用才可移植。
工具的 input_schema 就是 DraftBatch —— 也就是说，模型能填什么字段，
由类型系统而非提示词约束。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import Chunk, DraftBatch, DropRecord, TopicDraft

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

_TOOL = {
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
            tools=[_TOOL],
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
            drafts.append(
                TopicDraft(
                    draft_id=f"{chunk.id}-{index}",
                    chunk_id=chunk.id,
                    standard_codes=[chunk.standard_code],
                    content=content,
                )
            )

    return drafts, drops
