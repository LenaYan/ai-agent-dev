"""LangGraph 版编排（A 阶段：一层一个 Node，与手写版对等）。

与 run.py 的关系：**共用同一套六层纯函数**，只有编排方式不同。
这是路线图阶段四「同一需求分别用手写和框架实现」的框架半边。

Node 里只做「取 state → 调那层已有函数 → 返回 delta」，
不写任何业务逻辑 —— 否则对比就变成"手写版 vs 框架版+重构"，不公平。

已核实 API（langgraph 1.2.9，2026-07-26 实测）：节点签名 (state, runtime)，
运行时依赖走 runtime.context，不进 checkpoint。
"""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

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


def node_extract(state: PipelineState, runtime) -> dict:
    drafts, drops = extract_mod.extract_all(state["chunks"], runtime.context.extractor)
    io.write_stage(_out(state) / "02-drafts.json", drafts)
    io.append_drops(_out(state) / "dropped.json", drops)
    return {"drafts": drafts, "drops": drops}


def node_dedupe(state: PipelineState, runtime) -> dict:
    result = dedupe_mod.dedupe(state["drafts"], runtime.context.same_topic_judge)
    io.write_stage(_out(state) / "03-deduped.json", result.kept)
    io.write_stage(_out(state) / "merges.json", result.merges)
    io.append_drops(_out(state) / "dropped.json", result.drops)
    return {"deduped": result.kept, "merges": result.merges, "drops": result.drops}


def node_edges(state: PipelineState, runtime) -> dict:
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


def node_review(state: PipelineState, runtime) -> dict:
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
    deduped_edges, dup_drops = assemble_mod.dedupe_edges_by_pair(state["kept_edges"])
    io.append_drops(_out(state) / "dropped.json", dup_drops)
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
    return {"findings": findings, "drops": dup_drops}


def build_graph() -> StateGraph:
    g = StateGraph(PipelineState, context_schema=PipelineDeps)
    g.add_node("chunk", node_chunk)
    g.add_node("extract", node_extract)
    g.add_node("dedupe", node_dedupe)
    g.add_node("edges", node_edges)
    g.add_node("review", node_review)
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
) -> list[Finding]:
    """与 run.run_pipeline 行为对等的 LangGraph 版入口（不带 checkpointer）。

    checkpointer 与容错策略在 Task 5 加上 —— 先证明对等，再谈框架红利。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    app = build_graph().compile()
    result = app.invoke(
        {
            "source_dir": str(source_dir),
            "out_dir": str(out_dir),
            "model_id": model_id,
            "curriculum": curriculum,
            "drops": [],
        },
        context=deps,
    )
    # 注意：这里不再调用 io.append_drops —— 每个 Node 已经在自己返回 delta
    # 之前把 drops 落盘过一次（见 PipelineState 文档的取舍说明）。这里的
    # result["drops"] 是 reducer 聚合出的完整列表，只用于返回值，如果再
    # append 一次会让 dropped.json 里每条记录重复一份。
    return result["findings"]
