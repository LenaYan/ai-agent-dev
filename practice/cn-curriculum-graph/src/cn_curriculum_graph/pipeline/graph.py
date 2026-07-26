"""LangGraph 版编排（A 阶段：一层一个 Node，与手写版对等）。

与 run.py 的关系：**共用同一套六层纯函数**，只有编排方式不同。
这是路线图阶段四「同一需求分别用手写和框架实现」的框架半边。

Node 里只做「取 state → 调那层已有函数 → 返回 delta」，
不写任何业务逻辑 —— 否则对比就变成"手写版 vs 框架版+重构"，不公平。

已核实 API（langgraph 1.2.9，2026-07-26 实测）：节点签名 (state, runtime)，
运行时依赖走 runtime.context，不进 checkpoint。

Task 5 追加实测（同一 langgraph 1.2.9）：
- 每 Node `timeout` 只在**异步执行路径**上受支持——同步 `.invoke()` 落到的
  `pregel/_retry.py::run_with_retry` 只要 `task.timeout is not None` 就
  无条件 `raise sync_timeout_unsupported`，哪怕 Node 函数本身已经是
  `async def` 也一样；只有 `.ainvoke()` 落到的 `arun_with_retry` 才真正
  遵守 timeout。因此 extract/dedupe/edges/review 四个挂了
  `retry_policy`/`timeout` 的 Node 都改成了 `async def`（纯机制性改动，
  函数体没有 await、没有新增业务逻辑），且 `run_pipeline_lg` 内部改用
  `asyncio.run(app.ainvoke(...))`，对外仍保持同步签名。
- `SqliteSaver`（`langgraph.checkpoint.sqlite`）显式不支持 async 方法
  （`aget_tuple` 等直接 `raise NotImplementedError`，实测报错原文见
  `.superpowers/sdd/task-5-report.md`），既然编排走的是 ainvoke，
  checkpointer 必须用 `AsyncSqliteSaver`（`langgraph.checkpoint.sqlite.aio`，
  依赖 `aiosqlite`，已随 `langgraph-checkpoint-sqlite` 一并装好，
  pyproject 不需要再加一行）。这两点都与 Task 5 brief 原稿的代码不同，
  是被编译期/运行期报错逼出来的调整，不是我自己的偏好。
"""

from __future__ import annotations

import asyncio
import operator
from datetime import timedelta
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import chunk as chunk_mod
from cn_curriculum_graph.pipeline import dedupe as dedupe_mod
from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.models import (
    Chunk,
    DropRecord,
    Merge,
    ProposedEdge,
    ReviewOutcome,
    TargetedEdge,
    TopicDraft,
)
from cn_curriculum_graph.pipeline.run import DEFAULT_CURRICULUM, PipelineDeps
from cn_curriculum_graph.runner import run_all
from cn_curriculum_graph.validators.base import Finding, Severity

# 程序 bug 不该被重试 —— 重试三次只会把同一个 bug 犯三遍。
# 与手写版收窄 except 的策略同源（见 extract.py 的 PROGRAMMING_ERRORS）。
PROGRAMMING_ERRORS = (AttributeError, TypeError, NameError, KeyError)


def retry_on(exc: Exception) -> bool:
    return not isinstance(exc, PROGRAMMING_ERRORS)


RETRY_POLICY = RetryPolicy(max_attempts=3, retry_on=retry_on)
# 单个 LLM 层最长容忍时间。手写版**根本没有超时概念** —— 这一条是框架白送的。
# 公开命名（无下划线前缀）：graph_fanout.py 要跨模块复用它们。
NODE_TIMEOUT = timedelta(minutes=10)


class PipelineState(TypedDict, total=False):
    """drops 是唯一带 reducer 的字段：手写版有五处显式 io.append_drops，
    这里声明一次，reducer 就把"跨层累加"这件事白送了，累加语义成了类型的
    一部分，state 里的 `drops` 字段和最终返回值天然是完整聚合结果。

    但 reducer 只解决"聚合到哪"，不解决"什么时候落盘"——它只在 state
    这个内存结构里累加，state 什么时候被落到磁盘上，reducer 完全不管。
    早期实现图省事，把 io.append_drops 放在 run_pipeline_lg 末尾、
    graph.invoke 返回之后调一次：这在 happy path 下和手写版行为一致，
    但一旦中间某个 Node 抛异常，graph.invoke 直接向上抛、永远走不到那次
    末尾调用，dropped.json 就完全不会被创建 —— 哪怕前面的层已经产生过
    丢弃记录。手写版不会有这个问题，因为它是每层跑完立刻
    `io.append_drops`，写盘动作和"这层跑完了"绑在一起，不依赖"整条流水线
    跑到底"这个前提。

    修复：每个 Node 在返回 delta 之前，自己先把这一层产生的 drops 落盘
    （`io.append_drops`），时序上与手写版对齐；随后仍在 delta 里把 drops
    交给 reducer，供最终返回值和跨层守恒断言使用。这意味着 run_pipeline_lg
    末尾**不能**再调用一次 io.append_drops——否则每条记录会被写盘两次。
    也就是说，reducer 白送了"聚合"这个语义，但没有白送"每层跑完立刻
    持久化"这个时序保证——那部分依然得自己写，一行都不能省。这是这次
    A/B 对比里一条真实的取舍记录，不是"框架都帮你做好了"。
    """

    source_dir: str
    out_dir: str
    model_id: str
    curriculum: str

    chunks: list[Chunk]
    drafts: list[TopicDraft]
    deduped: list[TopicDraft]
    merges: list[Merge]
    proposed: dict[str, list[ProposedEdge]]
    reviewed: list[TopicDraft]
    kept_edges: dict[str, list[ProposedEdge]]
    outcomes: list[ReviewOutcome]
    findings: list[Finding]

    drops: Annotated[list[DropRecord], operator.add]


def _out(state: PipelineState) -> Path:
    return Path(state["out_dir"])


def node_chunk(state: PipelineState, runtime) -> dict:
    chunks: list[Chunk] = []
    drops: list[DropRecord] = []
    for path in sorted(Path(state["source_dir"]).glob("*.md")):
        produced, dropped = chunk_mod.split_source(
            path.read_text(encoding="utf-8"), source_file=path.name
        )
        chunks += produced
        drops += dropped
    io.write_stage(_out(state) / "01-chunks.json", chunks)
    # 立刻落盘，不等 run_pipeline_lg 末尾统一处理 —— 否则后面某层崩溃时
    # 这条记录会随进程一起消失，见 PipelineState 文档
    io.append_drops(_out(state) / "dropped.json", drops)
    return {"chunks": chunks, "drops": drops}


async def node_extract(state: PipelineState, runtime) -> dict:
    """async 是纯机制性的，不是业务逻辑：LangGraph 1.2.9 的 per-node timeout
    只支持 async node（同步节点在同一进程内没法被安全取消，见
    langgraph.pregel._utils.validate_timeout_supported）。这四个走 LLM 的
    Node 挂了 NODE_TIMEOUT，编译期就会因为函数是同步的而报
    ValueError（已实测），必须改成 async def 才能通过校验。函数体仍是
    「取 state → 调那层已有同步纯函数 → 返回 delta」，没有 await，
    不引入并发语义，也没有多写一行业务逻辑。
    """
    drafts, drops = extract_mod.extract_all(state["chunks"], runtime.context.extractor)
    io.write_stage(_out(state) / "02-drafts.json", drafts)
    io.append_drops(_out(state) / "dropped.json", drops)
    return {"drafts": drafts, "drops": drops}


async def node_dedupe(state: PipelineState, runtime) -> dict:
    result = dedupe_mod.dedupe(state["drafts"], runtime.context.same_topic_judge)
    io.write_stage(_out(state) / "03-deduped.json", result.kept)
    io.write_stage(_out(state) / "merges.json", result.merges)
    io.append_drops(_out(state) / "dropped.json", result.drops)
    return {"deduped": result.kept, "merges": result.merges, "drops": result.drops}


async def node_edges(state: PipelineState, runtime) -> dict:
    proposed, drops = edges_mod.propose_all(state["deduped"], runtime.context.edge_proposer)
    io.write_stage(
        _out(state) / "04-edges.json",
        [
            TargetedEdge(target_draft_id=target, edge=e)
            for target, group in proposed.items()
            for e in group
        ],
    )
    io.append_drops(_out(state) / "dropped.json", drops)
    return {"proposed": proposed, "drops": drops}


async def node_review(state: PipelineState, runtime) -> dict:
    deps = runtime.context
    draft_review = review_mod.review_drafts(
        state["deduped"], deps.fidelity_judges, deps.name_judges
    )
    kept_ids = {d.draft_id for d in draft_review.kept}
    surviving, prefilter_drops = review_mod.filter_edges_by_kept_drafts(
        state["proposed"], kept_ids
    )
    edge_review = review_mod.review_edges(
        {d.draft_id: d for d in draft_review.kept}, surviving, deps.edge_judges
    )
    orphan_drops = review_mod.detect_orphans(
        draft_review.kept, state["proposed"], edge_review.kept_edges
    )
    io.write_stage(_out(state) / "05-reviewed.json", draft_review.kept)
    io.write_stage(
        _out(state) / "review-log.json", draft_review.outcomes + edge_review.outcomes
    )
    review_drops = draft_review.drops + prefilter_drops + edge_review.drops + orphan_drops
    io.append_drops(_out(state) / "dropped.json", review_drops)
    return {
        "reviewed": draft_review.kept,
        "kept_edges": edge_review.kept_edges,
        "outcomes": draft_review.outcomes + edge_review.outcomes,
        "drops": review_drops,
    }


def node_assemble(state: PipelineState, runtime) -> dict:
    """**dup_drops 的落盘时机是任务描述的交互风险的具体落地点**：这里刻意把
    `io.append_drops` 挪到函数最末尾，而不是像早期实现那样紧跟在
    `dedupe_edges_by_pair` 之后就立刻写盘。

    原因：`assemble` 没有挂 `retry_policy`（纯规则层，LangGraph 不会自动
    重试），它唯一的"重跑"入口是调用方带着同一个 `checkpoint_db` /
    `thread_id` 再调一次 `run_pipeline_lg`。若在 `assemble_mod.assemble`
    / `run_all` 之前就把 `dup_drops` 落盘，一旦这两步之后失败，
    `node_assemble` 从未成功完成、checkpoint 里也就没有它的输出，下一次
    续跑会把这个函数从头整个重新执行一遍——`dup_drops` 会被重新算出、
    再 append 一次，dropped.json 里每条 DUPLICATE_EDGE 都会变成两份
    （已用 test_assemble_retry_via_checkpoint_does_not_duplicate_drops
    实测复现：修复前 2 条，见 task-5-report.md 的 RED 输出）。

    修复方式：让这一层的磁盘写入只发生在"整个 Node 都跑成功"之后，与
    extract/dedupe/edges/review 四层已经在用的模式完全一致——那四层的
    `io.write_stage`/`io.append_drops` 也全部放在函数体最后，这不是巧合，
    是同一个约束（"失败的尝试不留下任何部分写入，只有成功的那一次会写盘
    恰好一次"）在每一层都必须成立。这样无论 assemble 是被自动重试
    （它没有）还是被手工重新调用 `run_pipeline_lg` 恢复，失败的那次尝试
    在还没走到这一行之前就已经抛出，不会有任何写入；只有最终成功的那次
    调用会写一次。代价：`dup_drops` 不再是"算出来就立刻落盘"，如果
    `assemble_mod.assemble`/`run_all` 之后进程被 SIGKILL 之类硬杀（而不是
    Python 异常），这条记录会跟着丢——但那种情形下 `graph.json` 本身也
    没写完，重新跑一次全新的（未 resume 的）pipeline 才是正确应对，不依赖
    这条记录的持久性。
    """
    deduped_edges, dup_drops = assemble_mod.dedupe_edges_by_pair(state["kept_edges"])
    graph = assemble_mod.assemble(
        state["reviewed"],
        deduped_edges,
        model_id=state["model_id"],
        curriculum=state["curriculum"],
    )
    (_out(state) / "graph.json").write_text(
        graph.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
    )
    name_judges = runtime.context.name_judges
    findings = run_all(graph, judge=name_judges[0] if name_judges else None)
    if not graph.topics:
        findings.append(
            Finding(
                code="EMPTY_GENERATION",
                severity=Severity.ERROR,
                message=(
                    "本次生成没有产出任何知识点节点 —— 请查看 "
                    f"{_out(state) / 'dropped.json'} 定位是哪一层把输入全部丢弃了"
                ),
                context={
                    "chunks": len(state["chunks"]),
                    "drafts": len(state["drafts"]),
                    "deduped": len(state["deduped"]),
                    "reviewed": len(state["reviewed"]),
                },
            )
        )
    # 整个 Node 成功跑完之后才写盘一次——见函数顶部文档
    io.append_drops(_out(state) / "dropped.json", dup_drops)
    return {"findings": findings, "drops": dup_drops}


def build_graph() -> StateGraph:
    g = StateGraph(PipelineState, context_schema=PipelineDeps)
    g.add_node("chunk", node_chunk)
    g.add_node("extract", node_extract, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("dedupe", node_dedupe, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("edges", node_edges, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("review", node_review, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("assemble", node_assemble)
    g.add_edge(START, "chunk")
    g.add_edge("chunk", "extract")
    g.add_edge("extract", "dedupe")
    g.add_edge("dedupe", "edges")
    g.add_edge("edges", "review")
    g.add_edge("review", "assemble")
    g.add_edge("assemble", END)
    return g


def run_pipeline_lg(
    source_dir: Path,
    out_dir: Path,
    deps: PipelineDeps,
    model_id: str,
    curriculum: str = DEFAULT_CURRICULUM,
    checkpoint_db: Path | None = None,
    thread_id: str = "default",
) -> list[Finding]:
    """与 run.run_pipeline 行为对等的 LangGraph 版入口。

    `checkpoint_db` 为 None 时行为与 Task 4 一致（不带 checkpointer，每次
    从头跑）。传入路径后，同一 `thread_id` 第二次调用会从上次中断处续跑，
    不重跑已完成的 Node —— 手写版没有这个能力。

    **为什么内部用 `asyncio.run(...ainvoke(...))` 而不是 brief 原文的
    `.invoke(...)`**（已实测，非揣测）：langgraph 1.2.9 的每 Node
    `timeout` 只在异步执行路径上受支持。`.invoke()` 落到的同步执行器
    `pregel/_retry.py::run_with_retry` 只要 `task.timeout is not None`
    就无条件 `raise sync_timeout_unsupported`——即便 Node 函数本身已经是
    `async def`（本文件四个走 LLM 的 Node 均已如此，见 node_extract 的
    注释）。只有 `.ainvoke()` 落到的 `arun_with_retry` 才会真正遵守
    timeout。`run_pipeline_lg` 对外仍是同步函数（CLI/测试都同步调用），
    所以在函数体内用 `asyncio.run` 包一层，不改变对外签名。

    注意：这里**不**再调用一次 `io.append_drops(result["drops"])`。每个
    Node 已经在自己返回 delta 之前把这一层产生的 drops 落盘过一次（见
    PipelineState 文档的取舍说明）；`result["drops"]` 只是 reducer 聚合出的
    完整列表，用于返回值。若在这里再 append 一次，dropped.json 里每条记录
    都会重复一份 —— 这正是本任务要处理的交互风险之一，且与是否发生过重试
    / 续跑无关：哪怕全程零故障，这一条额外 append 也会让每条记录翻倍。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
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
            app = build_graph().compile()
            return await app.ainvoke(payload, config=config, context=deps)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
            app = build_graph().compile(checkpointer=saver)
            # 续跑：同一 thread_id 已有 checkpoint 时传 None，
            # LangGraph 会从上次中断处继续，而不是从头再来
            existing = await app.aget_state(config)
            resume = existing.next != ()
            return await app.ainvoke(None if resume else payload, config=config, context=deps)

    result = asyncio.run(_ainvoke())
    return result["findings"]
