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

from cn_curriculum_graph.judges.prompt import JUDGE_SYSTEM, build_user_message
from cn_curriculum_graph.validators.consistency import Verdict

# 名实一致判断是简单有界任务，1590 次调用也便宜；先便宜、量出来、再升级。
DEFAULT_MODEL = "claude-haiku-4-5"


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
            system=JUDGE_SYSTEM,
            messages=[{"role": "user", "content": build_user_message(name, description)}],
            output_format=Verdict,
        )
        return response.parsed_output
