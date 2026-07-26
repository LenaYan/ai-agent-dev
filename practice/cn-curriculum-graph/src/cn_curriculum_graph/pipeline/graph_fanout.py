"""LangGraph 版 B 阶段：把 extract 与 review 扇出到条目级。

**这已不是与手写版对等的实现** —— 架构变了。它回答的是另一个问题：
框架让我做成了原本不会去写的事吗？收益多大？

关键差异：A 阶段 checkpoint 粒度是"层"，429 恢复后重跑整层（含已成功的
条目）；B 阶段粒度是"条目"，只重跑失败那一条。这个差值就是第二章的数字。

dedupe 与 edges 不扇出：它们需要全局视野（配对要看到全部 draft、
候选前置要看到全部节点），扇开就不对了。

**与 task-7-brief.md 给定骨架代码的三处偏差**（都是被 langgraph 运行时/
类型系统逼出来的修正，不是我自己的偏好，已在报告里详细说明）：

1. `build_fanout_graph()` 的 `context_schema` 改成 `PipelineDeps`，不是
   brief 骨架里的 `type(None)`——`node_extract_one`/`node_dedupe`/
   `node_edges`/`_review_one`/`_review_collect`/`node_assemble` 全部要读
   `runtime.context.xxx`（extractor/judges 等），`context_schema=type(None)`
   会让 `runtime.context` 恒为 `None`，第一次访问 `.extractor` 就是
   `AttributeError`。
2. `node_extract_one` 改成 `async def` + `await asyncio.to_thread(...)`，
   不是骨架里的裸同步 `def`——同 `graph.py` C1 修复记录：`retry_policy`/
   `timeout` 只在异步执行路径上受支持，且函数体必须真的 `await` 才能让
   看门狗拿到调度机会，光有 `async def` 不够。`_review_one`/
   `_review_collect` 同理。
3. 骨架给的边列表里 `g.add_edge("edges", "review")` 是直连，但同时又注册了
   `"review_one"` 这个 Node——两者矛盾：如果 review 也要扇出到条目级，
   "edges" 之后必须先扇出到 `review_one`，再收敛到 `review`（对应
   `_review_collect`），而不是跳过 `review_one` 直连。这里补上
   `fan_out_review` + `g.add_conditional_edges("edges", fan_out_review,
   ["review_one"])` + `g.add_edge("review_one", "review")`。
4. `fan_out_chunks`/`fan_out_review` 都补了"扇出源为空列表"的兜底分支
   （直接路由到收敛节点，而不是返回 `[]`）——已实测踩坑：`Send` 扇出的
   路由完全由派发出去的 `Send` 任务触发，零个 `Send` 等价于这条边什么都
   没触发，下游收敛节点（`collect_extract`/`review`）以及再往后的
   `assemble` 全部不会执行，`run_pipeline_fanout` 取
   `result["findings"]` 会直接 `KeyError`。A 阶段的普通 Node 不会踩这个
   坑（哪怕输入为空也会被正常调用一次，返回空列表，流程照常走到
   assemble 的 EMPTY_GENERATION 兜底）——这是 Send 扇出相对普通 Node
   的一个真实代价，不是编造的边界情况。
"""

from __future__ import annotations

import asyncio
import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.graph import (
    NODE_TIMEOUT,
    RETRY_POLICY,
    PipelineState,
    _ensure_consistent_resume,
    node_assemble,
    node_chunk,
    node_dedupe,
    node_edges,
)
from cn_curriculum_graph.pipeline.models import Chunk, ReviewOutcome, TopicDraft
from cn_curriculum_graph.pipeline.run import DEFAULT_CURRICULUM, PipelineDeps
from cn_curriculum_graph.validators.base import Finding


class FanoutState(PipelineState, total=False):
    """B 阶段专用的 state schema：在 `PipelineState` 基础上追加两个累加字段，
    只在本文件里读写——**刻意不复用** A 阶段 `PipelineState` 里已有的
    `reviewed`/`outcomes`（那两个字段的语义是"最终结果，一次性覆盖写入"，
    由 `node_review`/`_review_collect` 在汇总阶段各写一次）。如果让
    `_review_one` 也直接累加进 `reviewed`/`outcomes`，等于给 A 阶段共用的
    `PipelineState` 追加 reducer 语义——A 阶段目前只有 `drafts` 被实测验证过
    "单点写入、reducer 与覆盖等价"，`reviewed`/`outcomes` 没有被这条论证
    覆盖到，节外生枝地扩大对共享 `PipelineState` 的改动面。新增两个独立
    字段、用子类而非直接改 `PipelineState`，两边字段的读写者完全不重叠，
    A 阶段的 `build_graph()`/`PipelineState` 一行未动。
    """

    draft_review_kept: Annotated[list[TopicDraft], operator.add]
    draft_review_outcomes: Annotated[list[ReviewOutcome], operator.add]


class _ExtractOne(TypedDict):
    """单条 chunk 的扇出输入。"""

    chunk: Chunk
    out_dir: str


class _ReviewOne(TypedDict):
    """单条 draft 的扇出输入。"""

    draft: TopicDraft
    out_dir: str


def _out(state: PipelineState) -> Path:
    return Path(state["out_dir"])


def fan_out_chunks(state: PipelineState) -> list[Send] | list[str]:
    """**空列表兜底（实测踩坑，非理论假设）**：`state["chunks"]` 为空时若
    直接返回 `[]`，一个 `Send` 任务都不会被派发，"collect_extract" 之后的
    整条链路（dedupe/edges/review/assemble）永远不会被触发——`ainvoke` 的
    返回值里完全没有 `findings` 键，`run_pipeline_fanout` 取
    `result["findings"]` 会 `KeyError`。已用
    `test_drops_from_a_failed_chunk_extraction_survive_on_disk`
    实测复现（0 draft 场景，虽然那条测试触发的是 review 侧的空列表，
    extract 侧同理会踩同一个坑，这里一并修）。A 阶段的 node_extract 不会有
    这个问题：它是普通 Node，哪怕 chunks 为空也会被正常调用一次、返回一个
    空列表，流程照常往下走到 assemble 的 EMPTY_GENERATION 兜底。B 阶段用
    `Send` 扇出后，"零个 Send"等价于"这条边什么都没触发"，必须显式判空、
    直接路由到收敛节点，才能保住"空输入仍能跑完整条流水线、由 assemble
    报 EMPTY_GENERATION"这条与 A 阶段一致的行为。
    """
    if not state["chunks"]:
        return ["collect_extract"]
    return [
        Send("extract_one", {"chunk": c, "out_dir": state["out_dir"]})
        for c in state["chunks"]
    ]


def fan_out_review(state: PipelineState) -> list[Send] | list[str]:
    """dedupe 之后逐 draft 扇出 fidelity + name_desc 判定——边审核仍需要
    全局 kept_ids，留在 `_review_collect` 里整批处理，不扇出。

    空列表兜底同 `fan_out_chunks`：`state["deduped"]` 为空时（例如全部
    chunk 都被抽取层丢弃）不能返回 `[]`，否则 "review" 收敛节点永远不会
    被触发，`assemble`/`findings` 全部跑不到。
    """
    if not state["deduped"]:
        return ["review"]
    return [
        Send("review_one", {"draft": d, "out_dir": state["out_dir"]})
        for d in state["deduped"]
    ]


async def node_extract_one(payload: _ExtractOne, runtime) -> dict:
    """一个 chunk 一次调用。失败只影响这一条 —— 这就是条目级 checkpoint 的价值：
    checkpoint 恢复时只重跑这一条 Send 任务，不像 A 阶段 node_extract 那样
    整层重跑（含已成功的条目）。

    `async def` + `await asyncio.to_thread(...)` 同 `graph.py` 里四个 LLM
    Node 的取舍：`retry_policy`/`timeout` 只在异步路径生效，且函数体必须
    真的让出控制权，看门狗才有机会介入。
    """
    drafts, drops = await asyncio.to_thread(
        extract_mod.extract_all, [payload["chunk"]], runtime.context.extractor
    )
    # 立刻落盘：不等 collect_extract 跑到——万一后面某个兄弟 Send 任务或
    # 下游节点崩溃，这一条已经成功的 chunk 的 drops 不会随进程一起消失。
    io.append_drops(Path(payload["out_dir"]) / "dropped.json", drops)
    return {"drafts": drafts, "drops": drops}


def _collect_extract(state: FanoutState, runtime) -> dict:
    """扇出的 drafts 已由 reducer（operator.add）汇总进 state["drafts"]，
    这里只负责把汇总结果落盘成 02-drafts.json，与 A 阶段 node_extract 的
    写盘时机对齐。drops 已经在各自的 node_extract_one 里落过盘，这里不
    重复 append。
    """
    io.write_stage(_out(state) / "02-drafts.json", state["drafts"])
    return {}


async def _review_one(payload: _ReviewOne, runtime) -> dict:
    """一个 draft 一次调用 review_drafts（fidelity + name_desc 两个维度）。
    对应 node_review 原本"逐 draft 判定"的那一半，边相关的部分（需要全局
    kept_ids）留给 _review_collect。

    失败/淘汰只影响这一条 draft —— 同 node_extract_one 的条目级价值：
    checkpoint 恢复时只重跑这一条 Send 任务。
    """
    deps = runtime.context
    result = await asyncio.to_thread(
        review_mod.review_drafts, [payload["draft"]], deps.fidelity_judges, deps.name_judges
    )
    io.append_drops(Path(payload["out_dir"]) / "dropped.json", result.drops)
    # 同 node_extract_one：落盘之后仍把 drops 塞进 delta，交给 `drops` 字段的
    # reducer——PipelineState 的文档明确写着"state 里的 drops 字段和最终
    # 返回值天然是完整聚合结果"，这是共享类型的既有不变量，不能因为
    # B 阶段多加了两个新字段就悄悄破坏它。
    return {
        "draft_review_kept": result.kept,
        "draft_review_outcomes": result.outcomes,
        "drops": result.drops,
    }


async def _review_collect(state: FanoutState, runtime) -> dict:
    """汇总 review_one 扇出的逐 draft 结果，接上 node_review 原本"边相关"的
    那一半：边预过滤 + 边审核 + 孤儿检测。这三步需要全局 kept_ids，
    不能扇出，直接复用 review.py 的六层函数，与 A 阶段 node_review 调用
    同一套函数、同样的落盘时序（先 append 边预过滤的 drops，再跑边审核，
    再检测孤儿）。
    """
    deps = runtime.context
    drops_path = _out(state) / "dropped.json"

    kept = state["draft_review_kept"]
    outcomes = state["draft_review_outcomes"]
    kept_ids = {d.draft_id for d in kept}

    surviving, prefilter_drops = review_mod.filter_edges_by_kept_drafts(
        state["proposed"], kept_ids
    )
    io.append_drops(drops_path, prefilter_drops)

    edge_review = await asyncio.to_thread(
        review_mod.review_edges,
        {d.draft_id: d for d in kept},
        surviving,
        deps.edge_judges,
    )
    io.append_drops(drops_path, edge_review.drops)

    orphan_drops = review_mod.detect_orphans(kept, state["proposed"], edge_review.kept_edges)
    io.append_drops(drops_path, orphan_drops)

    io.write_stage(_out(state) / "05-reviewed.json", kept)
    io.write_stage(_out(state) / "review-log.json", outcomes + edge_review.outcomes)

    review_drops = prefilter_drops + edge_review.drops + orphan_drops
    return {
        "reviewed": kept,
        "kept_edges": edge_review.kept_edges,
        "outcomes": outcomes + edge_review.outcomes,
        "drops": review_drops,
    }


def build_fanout_graph() -> StateGraph:
    g = StateGraph(FanoutState, context_schema=PipelineDeps)
    g.add_node("chunk", node_chunk)
    g.add_node("extract_one", node_extract_one, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("collect_extract", _collect_extract)
    g.add_node("dedupe", node_dedupe, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("edges", node_edges, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("review_one", _review_one, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("review", _review_collect, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("assemble", node_assemble)

    g.add_edge(START, "chunk")
    g.add_conditional_edges("chunk", fan_out_chunks, ["extract_one", "collect_extract"])
    g.add_edge("extract_one", "collect_extract")
    g.add_edge("collect_extract", "dedupe")
    g.add_edge("dedupe", "edges")
    g.add_conditional_edges("edges", fan_out_review, ["review_one", "review"])
    g.add_edge("review_one", "review")
    g.add_edge("review", "assemble")
    g.add_edge("assemble", END)
    return g


def run_pipeline_fanout(
    source_dir: Path,
    out_dir: Path,
    deps: PipelineDeps,
    model_id: str,
    curriculum: str = DEFAULT_CURRICULUM,
    checkpoint_db: Path | None = None,
    thread_id: str = "default",
) -> list[Finding]:
    """与 `run_pipeline_lg` 同签名、同一致性校验，唯一区别是编译
    `build_fanout_graph()` 而不是 `build_graph()`——checkpoint 粒度因此从
    "层"降到"条目"。

    这里复用 `graph.py` 的 `_ensure_consistent_resume`（同一份校验逻辑，
    不重新实现一遍），关于"为什么用 asyncio.run 包一层 ainvoke"、
    "为什么 checkpointer 必须是 AsyncSqliteSaver"这些取舍与
    `run_pipeline_lg` 完全相同，见该函数文档，这里不重复贴一遍论证。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if checkpoint_db is not None:
        checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_dir": str(source_dir),
        "out_dir": str(out_dir),
        "model_id": model_id,
        "curriculum": curriculum,
        "drops": [],
    }
    config = {"configurable": {"thread_id": thread_id}}

    async def _ainvoke() -> dict:
        if checkpoint_db is None:
            app = build_fanout_graph().compile()
            return await app.ainvoke(payload, config=config, context=deps)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
            app = build_fanout_graph().compile(checkpointer=saver)
            existing = await app.aget_state(config)
            resume = existing.next != ()
            if resume:
                _ensure_consistent_resume(existing.values, payload, thread_id=thread_id)
                return await app.ainvoke(None, config=config, context=deps)
            return await app.ainvoke(payload, config=config, context=deps)

    result = asyncio.run(_ainvoke())
    return result["findings"]
