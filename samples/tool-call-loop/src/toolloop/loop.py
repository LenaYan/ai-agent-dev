"""最小 agent 循环。

## 和 DAG 流水线的区别在哪一行

`practice/cn-curriculum-graph` 那种六层流水线，下一步做什么写在代码里：
    step2(step1(x)) -> step3(...)
控制流是**开发期就确定**的，模型只是每个节点里的一个函数调用。

这里的区别只在下面 `_step` 里的一行：

    if not reply.tool_calls:   # <-- 就是它
        return "done"

循环的退出条件、以及下一次调用哪个工具、调几次，都由**模型这一轮的输出**决定，
在运行期才知道。控制流从代码里搬到了模型输出里 —— 这就是 workflow 与 agent 的分界。

代价也在同一行：控制流不可预测 → 必须有 max_steps 兜底，必须记轨迹才能复盘。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from .llm import LLMClient, Reply
from .sandbox import Sandbox
from .tools import TOOL_SCHEMAS, dispatch

SYSTEM_PROMPT = """你是一个只能操作单个沙箱目录的助手。
你只能通过提供的工具了解和改动文件系统，不要凭空假设文件存在。
所有路径都是相对沙箱根目录的相对路径。
完成任务后，直接用一句话说明你做了什么，不要再调用工具。"""

StopReason = Literal["done", "max_steps"]


@dataclass
class Event:
    """轨迹里的一条记录。阶段三做轨迹评估时，评的就是这个列表。"""

    step: int
    kind: Literal["tool_call", "final"]
    name: str | None = None
    arguments: str | None = None
    result: str | None = None
    text: str | None = None


@dataclass
class Trajectory:
    events: list[Event] = field(default_factory=list)
    messages: list[dict[str, Any]] = field(default_factory=list)
    stop_reason: StopReason = "max_steps"
    final_text: str | None = None

    @property
    def tool_names(self) -> list[str]:
        return [e.name for e in self.events if e.kind == "tool_call" and e.name]

    @property
    def steps_used(self) -> int:
        return sum(1 for e in self.events if e.kind == "tool_call")


def run_agent(
    task: str,
    sandbox: Sandbox,
    client: LLMClient,
    max_steps: int = 12,
    verbose: bool = False,
) -> Trajectory:
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": task},
    ]
    traj = Trajectory(messages=messages)

    for step in range(1, max_steps + 1):
        reply: Reply = client.chat(messages, TOOL_SCHEMAS)
        messages.append(reply.raw)

        if not reply.tool_calls:
            traj.stop_reason = "done"
            traj.final_text = reply.text
            traj.events.append(Event(step=step, kind="final", text=reply.text))
            if verbose:
                print(f"[{step}] 结束: {reply.text}")
            return traj

        for call in reply.tool_calls:
            result = dispatch(sandbox, call.name, call.arguments)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": result,
                }
            )
            traj.events.append(
                Event(
                    step=step,
                    kind="tool_call",
                    name=call.name,
                    arguments=call.arguments,
                    result=result,
                )
            )
            if verbose:
                print(f"[{step}] {call.name}({call.arguments}) -> {result[:120]}")

    traj.stop_reason = "max_steps"
    return traj
