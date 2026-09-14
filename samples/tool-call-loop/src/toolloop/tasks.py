"""任务集：每个任务的成功判据都是**程序化的终态断言**。

这是 ADR-0007 的硬标准：不依赖人工标注。
`check()` 只看沙箱的最终状态，**不看 agent 说了什么** ——
模型说"我已完成"毫无分量，文件对了才算对。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from .sandbox import Sandbox

CheckResult = tuple[bool, str]


@dataclass(frozen=True)
class Task:
    name: str
    prompt: str
    setup: Callable[[Sandbox], None]
    check: Callable[[Sandbox], CheckResult]
    max_steps: int = 12


def _write(sandbox: Sandbox, path: str, content: str) -> None:
    target = sandbox.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")


# --- 任务 1：最少一次工具调用 -------------------------------------------------

def _setup_create_readme(sandbox: Sandbox) -> None:
    return None


def _check_create_readme(sandbox: Sandbox) -> CheckResult:
    path = sandbox.root / "README.md"
    if not path.is_file():
        return False, "README.md 不存在"
    first = path.read_text(encoding="utf-8").splitlines()[:1]
    if first != ["# Demo"]:
        return False, f"首行应为 '# Demo'，实际为 {first}"
    return True, "ok"


# --- 任务 2：必须先探索再汇总（多步，且步数不固定） ---------------------------

_NOTES = {
    "notes/a.txt": "alpha\nbeta\ngamma\n",
    "notes/b.txt": "one\ntwo\n",
    "notes/c.txt": "solo\n",
    "notes/skip.md": "不是 txt，不该被算进去\n",
}
_EXPECTED_LINES = 6


def _setup_count_lines(sandbox: Sandbox) -> None:
    for path, content in _NOTES.items():
        _write(sandbox, path, content)


def _check_count_lines(sandbox: Sandbox) -> CheckResult:
    path = sandbox.root / "total.txt"
    if not path.is_file():
        return False, "total.txt 不存在"
    text = path.read_text(encoding="utf-8").strip()
    if text != str(_EXPECTED_LINES):
        return False, f"total.txt 应为 '{_EXPECTED_LINES}'，实际为 {text!r}"
    for rel, content in _NOTES.items():
        if (sandbox.root / rel).read_text(encoding="utf-8") != content:
            return False, f"不该改动 {rel}"
    return True, "ok"


# --- 任务 3：读-改-写，且不许殃及其他内容 -------------------------------------

_CONFIG_BEFORE = "[app]\nname = demo\ndebug = true\nport = 8080\n"
_CONFIG_AFTER = "[app]\nname = demo\ndebug = false\nport = 8080\n"


def _setup_toggle_debug(sandbox: Sandbox) -> None:
    _write(sandbox, "config.ini", _CONFIG_BEFORE)


def _check_toggle_debug(sandbox: Sandbox) -> CheckResult:
    text = (sandbox.root / "config.ini").read_text(encoding="utf-8")
    if text != _CONFIG_AFTER:
        return False, f"config.ini 内容不对:\n{text!r}"
    return True, "ok"


TASKS: dict[str, Task] = {
    t.name: t
    for t in [
        Task(
            name="create_readme",
            prompt="在沙箱根目录创建 README.md，第一行写 '# Demo'。",
            setup=_setup_create_readme,
            check=_check_create_readme,
            max_steps=6,
        ),
        Task(
            name="count_lines",
            prompt=(
                "统计 notes/ 目录下所有 .txt 文件的总行数（不含 .md 文件），"
                "把这个数字写进沙箱根目录的 total.txt，文件里只放数字本身。"
            ),
            setup=_setup_count_lines,
            check=_check_count_lines,
            max_steps=12,
        ),
        Task(
            name="toggle_debug",
            prompt="把 config.ini 里的 debug 从 true 改成 false，其余内容一个字都不要动。",
            setup=_setup_toggle_debug,
            check=_check_toggle_debug,
            max_steps=8,
        ),
    ]
}
