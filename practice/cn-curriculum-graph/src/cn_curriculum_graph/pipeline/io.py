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


def _drop_dedup_key(record: dict) -> tuple:
    """去重键：`(stage, ref, reason, detail)` 全字段。

    去重边界（如实写清，不是保证）：若将来出现两条合法的、全字段相同的
    不同记录，它们会被折叠成一条——目前的原因码设计下 `ref`（通常是
    draft_id 或 "target<-prereq" 这种组合）足以唯一区分同一原因下的不同
    对象，但这只是当前设计的一个前提，不是这个函数能保证的不变量。
    """
    return (record.get("stage"), record.get("ref"), record.get("reason"), record.get("detail"))


def append_drops(path: Path, records: list[BaseModel]) -> None:
    """dropped.json 是跨层累加的，不是每层覆盖。

    **幂等**：按 `(stage, ref, reason, detail)` 全字段去重，重复写入同一条
    记录只会保留一条。

    **为什么需要幂等，以及为什么手写版从来不需要**：手写版 `run_pipeline`
    每层只调用一次 `append_drops`，不存在"同一条记录被写两次"的可能。
    这条幂等化是 LangGraph 版的 checkpoint 重入逼出来的——Node 失败后若
    整体重跑（例如 `assemble` 没有 `retry_policy`，唯一的恢复路径是带着
    同一个 `checkpoint_db`/`thread_id` 重新调用一次 `run_pipeline_lg`），
    Node 函数体会从头整个重新执行，其中的 drops 会被重新算出、再 append
    一次。若不做幂等，dropped.json 里每条记录都会变成两份（已用
    `test_assemble_retry_via_checkpoint_does_not_duplicate_drops` 实测
    复现）。这是这次 A/B 对比的一手素材：框架的 checkpoint 重入能力不是
    免费的，它把"持久化必须幂等"这条约束转嫁给了调用方——手写版因为从不
    重入，天然不需要付这个代价。
    """
    if not records:
        return
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.parent.mkdir(parents=True, exist_ok=True)
    seen = {_drop_dedup_key(r) for r in existing}
    payload = list(existing)
    for r in records:
        dumped = r.model_dump(mode="json")
        key = _drop_dedup_key(dumped)
        if key in seen:
            continue
        seen.add(key)
        payload.append(dumped)
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
