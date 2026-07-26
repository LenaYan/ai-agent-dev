"""编排六层，逐层落盘。

每层跑完就写文件再进下一层 —— 这不是为了性能，是为了**可人眼检查**。
看不见中间状态就没法判断它到底在干什么（effective-agents 心法③）。
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

from cn_curriculum_graph.judges.deepseek_judge import DeepSeekJudge
from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import chunk as chunk_mod
from cn_curriculum_graph.pipeline import dedupe as dedupe_mod
from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.models import Chunk, TargetedEdge, TopicDraft
from cn_curriculum_graph.runner import has_errors, run_all
from cn_curriculum_graph.validators.base import Finding

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
    surviving_edges = {
        target: [e for e in group if e.prerequisite_draft_id in kept_ids]
        for target, group in proposed.items()
        if target in kept_ids
    }
    edge_review = review_mod.review_edges(
        {d.draft_id: d for d in draft_review.kept}, surviving_edges, deps.edge_judges
    )
    io.append_drops(drops_path, edge_review.drops)
    io.write_stage(out_dir / "05-reviewed.json", draft_review.kept)
    io.write_stage(
        out_dir / "review-log.json", draft_review.outcomes + edge_review.outcomes
    )

    # 6 assemble + 校验
    graph = assemble_mod.assemble(
        draft_review.kept, edge_review.kept_edges, model_id=model_id, curriculum=curriculum
    )
    (out_dir / "graph.json").write_text(
        graph.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
    )
    return run_all(graph)


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
    args = parser.parse_args(argv)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("需要 DEEPSEEK_API_KEY（export 或写进 .env）")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    findings = run_pipeline(
        source_dir=args.source,
        out_dir=args.out,
        deps=build_deepseek_deps(models),
        model_id=models[0],
        curriculum=args.curriculum,
    )

    from cn_curriculum_graph.cli import format_report

    print(format_report(findings))
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
