"""端到端：全 fake 跑一遍完整管道，断言产出能过 run_all 且 0 error。

不为真模型的输出写断言 —— 内容正确性不在本轮承诺范围内，
为它写断言等于假装能验证。真模型的验证靠手动跑一次 + 人眼看中间产物。
"""

import json

from cn_curriculum_graph.pipeline.models import (
    DraftBatch,
    DraftContent,
    ProposedEdge,
    ProposedEdgeBatch,
    Vote,
)
from cn_curriculum_graph.pipeline.run import PipelineDeps, run_pipeline
from cn_curriculum_graph.runner import has_errors
from cn_curriculum_graph.validators.base import Severity
from cn_curriculum_graph.validators.consistency import Verdict

SOURCE = """3.1.1 能认识并读写 100 以内的数。

3.1.2 能计算 100 以内的加减法。
"""


def _content(name: str, grade: int, span: str) -> DraftContent:
    return DraftContent(
        name=name,
        description=f"{name}的具体内容",
        type="conceptual",
        subject="数学",
        domain="数与代数",
        grade_start=grade,
        grade_end=grade,
        evidence=[f"能演示{name}"],
        assessment_prompt=f"说说{name}？",
        source_span=span,
    )


def _fake_deps() -> PipelineDeps:
    def extractor(chunk):
        if chunk.standard_code == "3.1.1":
            return DraftBatch(drafts=[_content("认识100以内的数", 1, chunk.text)])
        return DraftBatch(drafts=[_content("100以内加减法", 2, chunk.text)])

    def same_topic(a, b):
        from cn_curriculum_graph.pipeline.dedupe import SameTopicVerdict

        return SameTopicVerdict(same=False, reason="不同")

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[
                ProposedEdge(
                    prerequisite_draft_id=candidates[0].draft_id,
                    strength="hard",
                    reason="先会读写才能算",
                )
            ]
        )

    return PipelineDeps(
        extractor=extractor,
        same_topic_judge=same_topic,
        edge_proposer=proposer,
        fidelity_judges=[lambda d: Vote(reviewer="fake", approved=True, reason="ok")],
        name_judges=[lambda name, description: Verdict(judgment="consistent")],
        edge_judges=[lambda t, e: Vote(reviewer="fake", approved=True, reason="ok")],
    )


def test_end_to_end_produces_a_graph_that_passes_validation(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    findings = run_pipeline(
        source_dir=source.parent,
        out_dir=out,
        deps=_fake_deps(),
        model_id="fake",
        curriculum="cn-moe-math-2022",
    )

    assert not has_errors(findings)


def test_every_stage_lands_a_readable_file(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    run_pipeline(source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    for name in (
        "01-chunks.json",
        "02-drafts.json",
        "03-deduped.json",
        "04-edges.json",
        "05-reviewed.json",
        "graph.json",
    ):
        assert (out / name).exists(), f"缺少中间产物 {name}"


def test_generated_graph_records_zero_confidence(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    run_pipeline(source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert all(t["provenance"]["confidence"] == 0.0 for t in graph["topics"])
    assert all(t["provenance"]["review_status"] == "unreviewed" for t in graph["topics"])


def test_dropped_records_accumulate_across_stages(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    # 第三段没有条目编号 —— chunk 层应当丢弃并记账
    source.write_text(SOURCE + "\n这一段是导言，没有编号。\n", encoding="utf-8")
    out = tmp_path / "out"

    run_pipeline(source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    assert any(d["reason"] == "NO_STANDARD_CODE" for d in drops)


def test_all_drafts_rejected_by_review_yields_empty_generation_error(tmp_path):
    """一次生成如果被 review 淘汰得一个节点都不剩，退出码不能装作没事。

    校验层（coverage.py 的 check_standards_coverage 等）面对空图会 early-return
    "无话可说" —— 那是校验层的合理语义（校验一份已有的图，空图确实没有校验规则
    可违反）。但"生成流水线跑出空图"是生成流水线自己的失败语义，得由 run_pipeline
    自己在 assemble 之后兜底，不能让空产出静默拿到 0 error。
    """
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    deps = _fake_deps()
    deps.fidelity_judges = [
        lambda d: Vote(reviewer="fake", approved=False, reason="全部否决")
    ]

    findings = run_pipeline(
        source.parent, out, deps, model_id="fake", curriculum="cn-moe-math-2022"
    )

    assert has_errors(findings)
    empty_findings = [f for f in findings if f.code == "EMPTY_GENERATION"]
    assert len(empty_findings) == 1
    finding = empty_findings[0]
    assert finding.severity is Severity.ERROR
    # context 要带够诊断信息，让人不用重新跑一遍就能定位是哪一层归零的
    for key in ("chunks", "drafts", "deduped", "reviewed"):
        assert key in finding.context


def test_normal_generation_does_not_emit_empty_generation_finding(tmp_path):
    """防误报：正常产出时不该背上 EMPTY_GENERATION。"""
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    findings = run_pipeline(
        source.parent, out, _fake_deps(), model_id="fake", curriculum="cn-moe-math-2022"
    )

    assert not any(f.code == "EMPTY_GENERATION" for f in findings)
