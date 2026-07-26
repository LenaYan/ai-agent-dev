"""DeepSeek 版的名实一致 judge，走其 Anthropic 兼容端点。

**为什么不直接复用 AnthropicJudge。** DeepSeek 的 `/anthropic` 端点确实能用同一个
`anthropic` SDK，但它**不遵守** `output_format`：实测照收参数、照样返回自由文本
（返回了一个"否"字），SDK 随后在 JSON 解析上炸掉。所以结构化输出改走另一条路 ——
**强制调用一个 `input_schema` 就是 Verdict 的工具**。这是跨 provider 通用的做法，
任何支持 tool use 的模型都能跑，代价是要自己做校验。

**为什么关 thinking。** DeepSeek v4 默认开思考模式，而思考模式下不接受强制
`tool_choice`（直接 400: "Thinking mode does not support this tool_choice"）。
名实一致是有界分类任务，也不需要思考预算。

**为什么用独立的 DEEPSEEK_API_KEY 而不是官方文档教的 ANTHROPIC_BASE_URL。**
那两个环境变量同机的 Claude Code 自己也在读，export 出去会把 Claude Code 整个
劫持到 DeepSeek 上。base_url 一律在代码里显式传。
"""

from __future__ import annotations

import os
from typing import Any

from cn_curriculum_graph.judges.prompt import JUDGE_SYSTEM, build_user_message
from cn_curriculum_graph.validators.consistency import Verdict

DEEPSEEK_BASE_URL = "https://api.deepseek.com/anthropic"
# 便宜档，中文语义是其主场；评测证明不够再升 deepseek-v4-pro（构造参数可覆盖）
DEFAULT_MODEL = "deepseek-v4-flash"
VERDICT_TOOL_NAME = "record_verdict"

# schema 由 Verdict 自己导出：手写一份迟早与模型定义漂移。
# Verdict 的 extra="forbid" 导出成 additionalProperties:false，正好收紧模型的自由度。
_VERDICT_TOOL = {
    "name": VERDICT_TOOL_NAME,
    "description": "记录『名称与描述是否讲同一件事』的判定结果",
    "input_schema": Verdict.model_json_schema(),
}


class DeepSeekJudge:
    """Judge 协议的 DeepSeek 实现。用法：`run_all(graph, judge=DeepSeekJudge())`。"""

    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic  # 懒加载：注入 client 的测试无需装 anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL,
                api_key=os.environ["DEEPSEEK_API_KEY"],
            )
        self._client = client
        self._model = model

    def __call__(self, name: str, description: str) -> Verdict:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=0,
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": build_user_message(name, description)}],
            tools=[_VERDICT_TOOL],
            tool_choice={"type": "tool", "name": VERDICT_TOOL_NAME},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == VERDICT_TOOL_NAME:
                # 强制 tool_choice 只保证"调了工具"，不保证参数合法，仍要过一遍 Verdict
                return Verdict.model_validate(block.input)
        raise ValueError(f"模型未调用 {VERDICT_TOOL_NAME} 工具，返回：{response.content!r}")
