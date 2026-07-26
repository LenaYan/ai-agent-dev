"""编排六层，逐层落盘。

每层跑完就写文件再进下一层 —— 这不是为了性能，是为了**可人眼检查**。
看不见中间状态就没法判断它到底在干什么（effective-agents 心法③）。
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

from cn_curriculum_graph.cli import format_report
from cn_curriculum_graph.judges.deepseek_judge import DeepSeekJudge
from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import chunk as chunk_mod
from cn_curriculum_graph.pipeline import dedupe as dedupe_mod
from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.models import (
    Chunk,
    TargetedEdge,
    TopicDraft,
)
from cn_curriculum_graph.runner import has_errors, run_all
from cn_curriculum_graph.validators.base import Finding, Severity

DEFAULT_CURRICULUM = "cn-moe-math-2022"
STAGES = ("chunk", "extract", "dedupe", "edges", "review", "assemble")


@dataclass
class PipelineDeps:
    """全部外部依赖集中在这里注入 —— 测试传 fake，生产传真 LLM。"""

    extractor: extract_mod.Extractor
    same_topic_judge: dedupe_mod.SameTopicJudge
    edge_proposer: edges_mod.EdgeProposer
    fidelity_judges: list[review_mod.FidelityJudge] = field(default_factory=list)
    name_judges: list = field(default_factory=list)
    edge_judges: list[review_mod.EdgeJudge] = field(default_factory=list)


def build_deepseek_deps(models: list[str]) -> PipelineDeps:
    """默认投票者是同族双票（flash + pro），独立性打折 —— 见 review.py 模块文档。"""
    primary = models[0]
    return PipelineDeps(
        extractor=extract_mod.DeepSeekExtractor(model=primary),
        same_topic_judge=dedupe_mod.DeepSeekSameTopicJudge(model=primary),
        edge_proposer=edges_mod.DeepSeekEdgeProposer(model=primary),
        fidelity_judges=[review_mod.DeepSeekFidelityJudge(model=m) for m in models],
        name_judges=[DeepSeekJudge(model=m) for m in models],
        edge_judges=[review_mod.DeepSeekEdgeJudge(model=m) for m in models],
    )


def run_pipeline(
    source_dir: Path,
    out_dir: Path,
    deps: PipelineDeps,
    model_id: str,
    curriculum: str = DEFAULT_CURRICULUM,
) -> list[Finding]:
    out_dir.mkdir(parents=True, exist_ok=True)
    drops_path = out_dir / "dropped.json"

    # 1 chunk
    chunks: list[Chunk] = []
    for path in sorted(source_dir.glob("*.md")):
        produced, dropped = chunk_mod.split_source(
            path.read_text(encoding="utf-8"), source_file=path.name
        )
        chunks += produced
        io.append_drops(drops_path, dropped)
    io.write_stage(out_dir / "01-chunks.json", chunks)

    # 2 extract
    drafts, dropped = extract_mod.extract_all(chunks, deps.extractor)
    io.append_drops(drops_path, dropped)
    io.write_stage(out_dir / "02-drafts.json", drafts)

    # 3 dedupe
    deduped = dedupe_mod.dedupe(drafts, deps.same_topic_judge)
    io.append_drops(drops_path, deduped.drops)
    io.write_stage(out_dir / "03-deduped.json", deduped.kept)
    io.write_stage(out_dir / "merges.json", deduped.merges)

    # 4 edges
    proposed, dropped = edges_mod.propose_all(deduped.kept, deps.edge_proposer)
    io.append_drops(drops_path, dropped)
    # 用 TargetedEdge 落盘：ProposedEdge 不带目标 id，直接摊平会读不出边属于谁
    io.write_stage(
        out_dir / "04-edges.json",
        [
            TargetedEdge(target_draft_id=target, edge=e)
            for target, group in proposed.items()
            for e in group
        ],
    )

    # 5 review
    draft_review = review_mod.review_drafts(
        deduped.kept, deps.fidelity_judges, deps.name_judges
    )
    io.append_drops(drops_path, draft_review.drops)
    kept_ids = {d.draft_id for d in draft_review.kept}

    # 边预过滤：逻辑抽进 review.filter_edges_by_kept_drafts，两个编排实现共用，
    # 避免复制一份后改一处漏一处
    surviving_edges, edge_prefilter_drops = review_mod.filter_edges_by_kept_drafts(
        proposed, kept_ids
    )
    io.append_drops(drops_path, edge_prefilter_drops)

    edge_review = review_mod.review_edges(
        {d.draft_id: d for d in draft_review.kept}, surviving_edges, deps.edge_judges
    )
    io.append_drops(drops_path, edge_review.drops)

    # 淘汰会制造孤儿：原本有前置的节点，前置全被淘汰后就静默失去了依赖。
    # 只记账不丢弃 —— 节点本身没问题，问题在于它的前置没了。
    io.append_drops(
        drops_path,
        review_mod.detect_orphans(draft_review.kept, proposed, edge_review.kept_edges),
    )

    io.write_stage(out_dir / "05-reviewed.json", draft_review.kept)
    io.write_stage(
        out_dir / "review-log.json", draft_review.outcomes + edge_review.outcomes
    )

    # 待裁决 #5：同一 (target, prerequisite) 若被给出 hard + soft 两条边，
    # assemble 之前先去重，hard 胜 soft，并把丢弃的那条记进 dropped.json ——
    # 详见 assemble.dedupe_edges_by_pair 的 docstring（选择在编排层做，
    # 而不是让 assemble 静默去重，是因为 assemble 没有 DropRecord 通道，
    # 静默去重会又制造一个"没有留痕的丢弃"）。
    deduped_edges, duplicate_edge_drops = assemble_mod.dedupe_edges_by_pair(
        edge_review.kept_edges
    )
    io.append_drops(drops_path, duplicate_edge_drops)

    # 6 assemble + 校验
    graph = assemble_mod.assemble(
        draft_review.kept, deduped_edges, model_id=model_id, curriculum=curriculum
    )
    (out_dir / "graph.json").write_text(
        graph.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
    )
    # review 层已经用 name judge 跑过名实一致，把它传给 run_all，
    # 否则最终报告会打印 CONSISTENCY_SKIPPED 说"已跳过"——
    # 这条留痕机制自己出的岔子，比不留痕更误导人。
    findings = run_all(graph, judge=deps.name_judges[0] if deps.name_judges else None)

    # 空产出兜底：这条检查刻意放在 run_pipeline 而不是校验层。
    # 校验层（validators/coverage.py 等）的职责是"校验一份已有的图"，
    # 空图对它是合法输入，没有规则可违反，因此各校验器面对空图都会
    # early-return（如 check_standards_coverage 的 `if not graph.topics:
    # return []`）——这本身没错。但"生成流水线跑出一份空图"是生成
    # 流水线自己的失败语义：本该产出知识点，结果一个没剩，这不是
    # "无话可说"，是彻头彻尾的失败。把这条检查塞进校验层会污染它
    # "只校验已有图"的单一职责，所以放在这里，由 run_pipeline 自己兜底。
    if not graph.topics:
        findings.append(
            Finding(
                code="EMPTY_GENERATION",
                severity=Severity.ERROR,
                message=(
                    "本次生成没有产出任何知识点节点 —— 请查看 "
                    f"{drops_path} 定位是哪一层把输入全部丢弃了"
                ),
                context={
                    "chunks": len(chunks),
                    "drafts": len(drafts),
                    "deduped": len(deduped.kept),
                    "reviewed": len(draft_review.kept),
                },
            )
        )

    return findings


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccg-generate", description="从课标原文生成知识依赖图"
    )
    parser.add_argument("--source", type=Path, default=Path("data/source"))
    parser.add_argument("--out", type=Path, default=Path("data/generated"))
    parser.add_argument("--curriculum", default=DEFAULT_CURRICULUM)
    parser.add_argument(
        "--models",
        default="deepseek-v4-flash,deepseek-v4-pro",
        help="审核投票者，逗号分隔；第一个同时用于抽取与连边",
    )
    parser.add_argument(
        "--engine",
        choices=("handwritten", "langgraph"),
        default="handwritten",
        help="编排实现。两者行为对等，langgraph 版额外支持 --checkpoint 断点续跑",
    )
    parser.add_argument(
        "--checkpoint", type=Path, default=None, help="checkpoint 数据库路径（仅 langgraph）"
    )
    args = parser.parse_args(argv)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("需要 DEEPSEEK_API_KEY（export 或写进 .env）")

    models = [m.strip() for m in args.models.split(",") if m.strip()]

    if args.engine == "langgraph":
        from cn_curriculum_graph.pipeline.graph import run_pipeline_lg

        findings = run_pipeline_lg(
            args.source, args.out, build_deepseek_deps(models),
            model_id=models[0], curriculum=args.curriculum,
            checkpoint_db=args.checkpoint,
        )
    else:
        if args.checkpoint is not None:
            parser.error("--checkpoint 仅 --engine langgraph 支持（手写版没有重入能力，这正是对比要量的东西）")
        findings = run_pipeline(
            args.source, args.out, build_deepseek_deps(models),
            model_id=models[0], curriculum=args.curriculum,
        )

    print(format_report(findings))
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
