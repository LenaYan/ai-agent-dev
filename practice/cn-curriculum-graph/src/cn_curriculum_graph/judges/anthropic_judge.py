"""用真 LLM 判定 name 与 description 是否讲的是同一件事。

实现 `validators.consistency.Judge` 协议（`(name, description) -> Verdict`），
接进 `runner.run_all(graph, judge=...)` 即可激活 NAME_DESC_MISMATCH。

三个设计点：
1. **client 依赖注入**：构造时可传入自己的 Anthropic client；不传则懒加载一个默认的。
   测试注入 fake client，因而无需 API key、不触网。
2. **结构化输出**：用 `messages.parse(output_format=Verdict)` 逼模型直接返回
   `{consistent, reason}`，而不是自由文本再解析。Verdict 的 `extra="forbid"`
   恰好满足结构化输出要求的 `additionalProperties: false`。
3. **temperature=0**：名实一致是有界分类任务，要可复现。
   默认模型 Haiku 4.5 —— 便宜、够用；评测证明不够再升级（构造参数可覆盖）。
"""

from __future__ import annotations

from typing import Any

from cn_curriculum_graph.validators.consistency import Verdict

# 名实一致判断是简单有界任务，1590 次调用也便宜；先便宜、量出来、再升级。
DEFAULT_MODEL = "claude-haiku-4-5"

_SYSTEM = (
    "你是小学课标知识依赖图的数据质检员。"
    "会给你一个知识点的『名称』和『描述』，判断二者讲的是不是同一件事。\n"
    "判 consistent=false 的情形：名称说的概念与描述实际教的内容属于不同知识点"
    "（例如名称写『乘法阵列』而描述在讲短除法，或名称写『认识角』而描述在求面积）。\n"
    "判 consistent=true 的情形：名称是描述内容的合理概括，即便措辞、语言不同"
    "（中英文、宽泛与具体）也算一致。\n"
    "只做名称与描述的一致性判断，不评价描述本身对不对、不看其他字段。\n"
    "reason 用一句中文说明依据。"
)


class AnthropicJudge:
    """Judge 协议的 LLM 实现。用法：`run_all(graph, judge=AnthropicJudge())`。"""

    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic  # 懒加载：注入 client 的测试无需装 anthropic

            client = anthropic.Anthropic()
        self._client = client
        self._model = model

    def __call__(self, name: str, description: str) -> Verdict:
        response = self._client.messages.parse(
            model=self._model,
            max_tokens=512,
            temperature=0,
            system=_SYSTEM,
            messages=[
                {
                    "role": "user",
                    "content": f"名称：{name}\n\n描述：{description}",
                }
            ],
            output_format=Verdict,
        )
        return response.parsed_output
