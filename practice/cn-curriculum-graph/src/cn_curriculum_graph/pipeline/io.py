"""分层落盘。

每层产物单独成文件，目的有两个：可从任意层重入（`--from`），
以及**可人眼检查**。第二条是 effective-agents 心法③"像你的 Agent 一样思考"的
直接落实 —— 看不见中间状态就没法判断它到底在干什么。故 JSON 一律
ensure_ascii=False + indent=2，宁可文件大一点也要能读。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def write_stage(path: Path, items: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [item.model_dump(mode="json") for item in items]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_stage(path: Path, model: type[T]) -> list[T]:
    """文件不存在时返回空列表 —— 上一层可能一条都没产出，这不是错误。"""
    if not path.exists():
        return []
    return [model.model_validate(raw) for raw in json.loads(path.read_text(encoding="utf-8"))]


def append_drops(path: Path, records: list[BaseModel]) -> None:
    """dropped.json 是跨层累加的，不是每层覆盖。"""
    if not records:
        return
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = existing + [r.model_dump(mode="json") for r in records]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
