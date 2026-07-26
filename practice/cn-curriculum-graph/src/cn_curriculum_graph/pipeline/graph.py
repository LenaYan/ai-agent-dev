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
    这里声明一次，累加语义成了类型的一部分。

    代价是丢了"每层跑完立刻落盘"的时序保证 —— 所以每个 Node 仍显式调
    io.write_stage，中间产物可人眼检查是项目原则，不是手写版的实现细节。
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
    return {"chunks": chunks, "drops": drops}


def node_extract(state: PipelineState, runtime) -> dict:
    drafts, drops = extract_mod.extract_all(state["chunks"], runtime.context.extractor)
    io.write_stage(_out(state) / "02-drafts.json", drafts)
    return {"drafts": drafts, "drops": drops}


def node_dedupe(state: PipelineState, runtime) -> dict:
    result = dedupe_mod.dedupe(state["drafts"], runtime.context.same_topic_judge)
    io.write_stage(_out(state) / "03-deduped.json", result.kept)
    io.write_stage(_out(state) / "merges.json", result.merges)
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
    return {
        "reviewed": draft_review.kept,
        "kept_edges": edge_review.kept_edges,
        "outcomes": draft_review.outcomes + edge_review.outcomes,
        "drops": draft_review.drops + prefilter_drops + edge_review.drops + orphan_drops,
    }


def node_assemble(state: PipelineState, runtime) -> dict:
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
    io.append_drops(out_dir / "dropped.json", result["drops"])
    return result["findings"]
