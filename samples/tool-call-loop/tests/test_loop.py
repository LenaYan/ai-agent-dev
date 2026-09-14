"""用 ScriptedLLM 验收循环本身，不依赖任何真实模型。

测的是循环的性质，不是模型的聪明程度：
- 工具结果有没有正确回灌（tool_call_id 对得上）
- 模型不再调工具时是否停
- 步数用尽时是否被 max_steps 拦住
- 工具报错是否变成消息而不是异常
- 沙箱越界是否被挡
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from toolloop.llm import ScriptedLLM, scripted_text, scripted_tool_call
from toolloop.loop import run_agent
from toolloop.sandbox import Sandbox, SandboxViolation
from toolloop.tasks import TASKS


@pytest.fixture()
def sandbox(tmp_path: Path) -> Sandbox:
    return Sandbox.create(tmp_path / "box")


def test_stops_when_model_returns_no_tool_calls(sandbox: Sandbox) -> None:
    client = ScriptedLLM([scripted_text("没什么要做的")])
    traj = run_agent("随便", sandbox, client, max_steps=5)
    assert traj.stop_reason == "done"
    assert traj.steps_used == 0
    assert traj.final_text == "没什么要做的"


def test_tool_result_is_fed_back_with_matching_id(sandbox: Sandbox) -> None:
    client = ScriptedLLM(
        [
            scripted_tool_call("write_file", call_id="abc", path="a.txt", content="hi"),
            scripted_text("写好了"),
        ]
    )
    traj = run_agent("写 a.txt", sandbox, client, max_steps=5)

    assert (sandbox.root / "a.txt").read_text(encoding="utf-8") == "hi"
    tool_msgs = [m for m in traj.messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert tool_msgs[0]["tool_call_id"] == "abc"
    assert "已写入" in tool_msgs[0]["content"]
    # 第二次调用模型时，它必须已经看到工具结果
    assert any(m.get("role") == "tool" for m in client.calls[1])


def test_max_steps_stops_a_runaway_loop(sandbox: Sandbox) -> None:
    client = ScriptedLLM([scripted_tool_call("list_files", path=".") for _ in range(10)])
    traj = run_agent("死循环", sandbox, client, max_steps=3)
    assert traj.stop_reason == "max_steps"
    assert traj.steps_used == 3


def test_tool_failure_becomes_a_message_not_an_exception(sandbox: Sandbox) -> None:
    client = ScriptedLLM(
        [
            scripted_tool_call("read_file", path="不存在.txt"),
            scripted_tool_call("no_such_tool"),
            scripted_text("放弃"),
        ]
    )
    traj = run_agent("读个不存在的文件", sandbox, client, max_steps=5)
    results = [e.result for e in traj.events if e.kind == "tool_call"]
    assert "文件不存在" in results[0]
    assert "没有名为" in results[1]
    assert traj.stop_reason == "done"


def test_bad_json_arguments_do_not_crash(sandbox: Sandbox) -> None:
    from toolloop.tools import dispatch

    assert "不是合法 JSON" in dispatch(sandbox, "read_file", "{not json")
    assert "参数不匹配" in dispatch(sandbox, "read_file", json.dumps({"wrong": 1}))


def test_sandbox_blocks_escape(sandbox: Sandbox) -> None:
    with pytest.raises(SandboxViolation):
        sandbox.resolve("../../etc/passwd")
    from toolloop.tools import dispatch

    assert "路径越界" in dispatch(
        sandbox, "write_file", json.dumps({"path": "../evil.txt", "content": "x"})
    )


def test_task_checks_reject_wrong_final_state(tmp_path: Path) -> None:
    """判据本身要能判错 —— 否则全 PASS 毫无意义（2026-07-28 的教训）。"""
    for name, task in TASKS.items():
        box = Sandbox.create(tmp_path / f"neg-{name}")
        task.setup(box)
        ok, _ = task.check(box)
        assert ok is False, f"{name} 在未做任何事时竟然判定为通过"


def test_count_lines_task_passes_with_a_correct_trajectory(tmp_path: Path) -> None:
    task = TASKS["count_lines"]
    box = Sandbox.create(tmp_path / "count")
    task.setup(box)
    client = ScriptedLLM(
        [
            scripted_tool_call("list_files", call_id="c1", path="notes"),
            scripted_tool_call("read_file", call_id="c2", path="notes/a.txt"),
            scripted_tool_call("read_file", call_id="c3", path="notes/b.txt"),
            scripted_tool_call("read_file", call_id="c4", path="notes/c.txt"),
            scripted_tool_call("write_file", call_id="c5", path="total.txt", content="6"),
            scripted_text("总共 6 行"),
        ]
    )
    traj = run_agent(task.prompt, box, client, max_steps=task.max_steps)
    ok, detail = task.check(box)
    assert ok, detail
    assert traj.tool_names == [
        "list_files",
        "read_file",
        "read_file",
        "read_file",
        "write_file",
    ]
