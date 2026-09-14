"""LLM 客户端：一个协议 + 两个实现。

为什么不直接 `import openai`：
阶段一要学的正是 tool calling 的**线上格式**本身 —— 谁在描述工具、
模型怎么表达"我要调这个"、结果怎么回灌。SDK 会把这层包起来。
这里用 stdlib 直接发 HTTP，整个 sample 零运行时依赖。

`OpenAICompatClient` 对准的是 OpenAI 的 /chat/completions 协议，
Ollama、OpenRouter、DeepSeek、vLLM、LM Studio 都实现了它，换后端只改 .env。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class ToolCall:
    id: str
    name: str
    arguments: str  # 模型给的是 JSON 字符串，不是 dict


@dataclass(frozen=True)
class Reply:
    text: str | None
    tool_calls: list[ToolCall] = field(default_factory=list)
    raw: dict[str, Any] = field(default_factory=dict)
    """raw 是模型返回的原始 assistant 消息。

    必须原样塞回 messages —— tool_calls 的 id 要和后续 tool 消息对上，
    自己重建这个 dict 是踩坑重灾区。
    """


class LLMClient(Protocol):
    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Reply: ...


def _parse_choice(message: dict[str, Any]) -> Reply:
    calls = [
        ToolCall(
            id=tc.get("id") or f"call_{i}",
            name=tc["function"]["name"],
            arguments=tc["function"].get("arguments") or "{}",
        )
        for i, tc in enumerate(message.get("tool_calls") or [])
    ]
    return Reply(text=message.get("content"), tool_calls=calls, raw=message)


class LLMError(RuntimeError):
    pass


@dataclass
class OpenAICompatClient:
    model: str
    base_url: str
    api_key: str = ""
    temperature: float = 0.0
    timeout: float = 120.0

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Reply:
        payload = {
            "model": self.model,
            "messages": messages,
            "tools": tools,
            "temperature": self.temperature,
        }
        req = urllib.request.Request(
            url=f"{self.base_url.rstrip('/')}/chat/completions",
            data=json.dumps(payload).encode("utf-8"),
            headers={
                "Content-Type": "application/json",
                **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {}),
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raise LLMError(
                f"HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:500]}"
            ) from exc
        except urllib.error.URLError as exc:
            raise LLMError(f"连不上 {self.base_url}: {exc.reason}") from exc
        try:
            return _parse_choice(body["choices"][0]["message"])
        except (KeyError, IndexError) as exc:
            raise LLMError(f"返回结构不认识: {json.dumps(body)[:500]}") from exc


@dataclass
class ScriptedLLM:
    """按剧本逐条返回的假模型。

    它存在的理由不是"没有 key 时的替代品"，而是**让循环本身可测**：
    循环的正确性（工具结果怎么回灌、什么时候停）不该依赖一次真实的模型采样。
    """

    script: list[Reply]
    calls: list[list[dict[str, Any]]] = field(default_factory=list)

    def chat(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> Reply:
        self.calls.append([dict(m) for m in messages])
        if not self.script:
            raise LLMError("剧本已用完，但循环还在要下一步")
        return self.script.pop(0)


def scripted_tool_call(name: str, call_id: str = "call_1", **arguments: Any) -> Reply:
    raw = {
        "role": "assistant",
        "content": None,
        "tool_calls": [
            {
                "id": call_id,
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    }
    return _parse_choice(raw)


def scripted_text(text: str) -> Reply:
    return _parse_choice({"role": "assistant", "content": text})
