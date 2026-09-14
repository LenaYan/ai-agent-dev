"""工具层：函数 + 给模型看的 JSON Schema + 分发。

三件事必须分开看：
1. Python 函数本身（真正干活的）
2. 工具的 schema（模型唯一能看到的东西 —— 描述写不好，模型就选不对）
3. dispatch（把模型返回的 JSON 参数喂给函数，并把异常变成"工具返回"而不是崩溃）

第 3 点是新手最容易写错的地方：工具失败必须作为**普通的 tool 消息**回给模型，
让它自己决定重试还是换路。抛出去中断循环，就退化成了人写死控制流。
"""

from __future__ import annotations

import json
from typing import Any, Callable

from .sandbox import Sandbox, SandboxViolation

MAX_READ_CHARS = 4000

TOOL_SCHEMAS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_files",
            "description": "列出沙箱内某个目录下的文件与子目录（相对路径）。不确定有什么文件时先调它。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {
                        "type": "string",
                        "description": "相对沙箱根目录的路径，根目录用 '.'",
                    }
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": f"读取沙箱内一个文本文件的内容，最多返回前 {MAX_READ_CHARS} 个字符。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对沙箱根目录的文件路径"}
                },
                "required": ["path"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "write_file",
            "description": "把内容整体写入沙箱内的文件（覆盖已有内容，父目录自动创建）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "相对沙箱根目录的文件路径"},
                    "content": {"type": "string", "description": "要写入的完整文本内容"},
                },
                "required": ["path", "content"],
            },
        },
    },
]


def _list_files(sandbox: Sandbox, path: str = ".") -> str:
    target = sandbox.resolve(path)
    if not target.is_dir():
        return f"错误: {path} 不是目录"
    entries = sorted(
        f"{p.name}/" if p.is_dir() else p.name for p in target.iterdir()
    )
    return "\n".join(entries) if entries else "(空目录)"


def _read_file(sandbox: Sandbox, path: str) -> str:
    target = sandbox.resolve(path)
    if not target.is_file():
        return f"错误: 文件不存在 {path}"
    text = target.read_text(encoding="utf-8")
    if len(text) > MAX_READ_CHARS:
        return text[:MAX_READ_CHARS] + f"\n...(已截断，共 {len(text)} 字符)"
    return text


def _write_file(sandbox: Sandbox, path: str, content: str) -> str:
    target = sandbox.resolve(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return f"已写入 {path}（{len(content)} 字符）"


REGISTRY: dict[str, Callable[..., str]] = {
    "list_files": _list_files,
    "read_file": _read_file,
    "write_file": _write_file,
}


def dispatch(sandbox: Sandbox, name: str, arguments: str) -> str:
    """执行一次工具调用，**任何错误都变成字符串返回给模型**。"""
    fn = REGISTRY.get(name)
    if fn is None:
        return f"错误: 没有名为 {name} 的工具，可用工具: {', '.join(REGISTRY)}"
    try:
        kwargs = json.loads(arguments) if arguments else {}
    except json.JSONDecodeError as exc:
        return f"错误: 参数不是合法 JSON ({exc})"
    if not isinstance(kwargs, dict):
        return "错误: 参数必须是 JSON 对象"
    try:
        return fn(sandbox, **kwargs)
    except SandboxViolation as exc:
        return f"错误: {exc}"
    except TypeError as exc:
        return f"错误: 参数不匹配 ({exc})"
    except OSError as exc:
        return f"错误: 文件操作失败 ({exc})"
