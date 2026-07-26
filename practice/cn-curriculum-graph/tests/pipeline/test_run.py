"""端到端：全 fake 跑一遍完整管道，断言产出能过 run_all 且 0 error。

不为真模型的输出写断言 —— 内容正确性不在本轮承诺范围内，
为它写断言等于假装能验证。真模型的验证靠手动跑一次 + 人眼看中间产物。
"""

import json

import pytest

from cn_curriculum_graph.pipeline.dedupe import SameTopicVerdict
from cn_curriculum_graph.pipeline.graph import run_pipeline_lg
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


def _run(engine: str, source_dir, out_dir, deps, model_id, curriculum):
    """两个编排实现的统一入口。测试对实现无感知，才谈得上『对等』。"""
    if engine == "handwritten":
        return run_pipeline(source_dir, out_dir, deps, model_id=model_id, curriculum=curriculum)
    if engine == "langgraph":
        return run_pipeline_lg(source_dir, out_dir, deps, model_id=model_id, curriculum=curriculum)
    raise ValueError(f"未知引擎：{engine}")


ENGINES = pytest.mark.parametrize("engine", ["handwritten", "langgraph"])


def _content(name: str, grade: int, span: str, evidence: list[str] | None = None) -> DraftContent:
    return DraftContent(
        name=name,
        description=f"{name}的具体内容",
        type="conceptual",
        subject="数学",
        domain="数与代数",
        grade_start=grade,
        grade_end=grade,
        evidence=evidence or [f"能演示{name}"],
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


def _deps_rejecting(rejected_name: str) -> PipelineDeps:
    """除指定名称外全部通过 fidelity 的 deps，用于制造"基础节点被淘汰"场景。"""
    deps = _fake_deps()
    deps.fidelity_judges = [
        lambda d: Vote(
            reviewer="fake", approved=d.content.name != rejected_name, reason="测试"
        )
    ]
    return deps


@ENGINES
def test_end_to_end_produces_a_graph_that_passes_validation(tmp_path, engine):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    findings = _run(
        engine,
        source.parent,
        out,
        _fake_deps(),
        model_id="fake",
        curriculum="cn-moe-math-2022",
    )

    assert not has_errors(findings)


@ENGINES
def test_every_stage_lands_a_readable_file(tmp_path, engine):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    _run(engine, source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    for name in (
        "01-chunks.json",
        "02-drafts.json",
        "03-deduped.json",
        "04-edges.json",
        "05-reviewed.json",
        "graph.json",
    ):
        assert (out / name).exists(), f"缺少中间产物 {name}"


@ENGINES
def test_generated_graph_records_zero_confidence(tmp_path, engine):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    _run(engine, source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert all(t["provenance"]["confidence"] == 0.0 for t in graph["topics"])
    assert all(t["provenance"]["review_status"] == "unreviewed" for t in graph["topics"])


@ENGINES
def test_dropped_records_accumulate_across_stages(tmp_path, engine):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    # 第三段没有条目编号 —— chunk 层应当丢弃并记账
    source.write_text(SOURCE + "\n这一段是导言，没有编号。\n", encoding="utf-8")
    out = tmp_path / "out"

    _run(engine, source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    assert any(d["reason"] == "NO_STANDARD_CODE" for d in drops)


@ENGINES
def test_dropped_json_survives_a_crash_mid_pipeline(tmp_path, engine, monkeypatch):
    """崩溃复原实验的前提条件：两个引擎在崩溃现场的产物必须一致，否则下一个
    任务对比的是这个混淆变量，不是框架本身的差异。

    用 monkeypatch 让 review 层抛异常模拟"跑到一半崩了"，断言 dropped.json
    在崩溃之后依然存在于磁盘上，且包含崩溃前（chunk 层）已经产生的记录 ——
    不能因为记账机制没来得及在末尾一次性写盘，就让"崩溃现场"被误读成
    "零丢弃"。
    """
    from cn_curriculum_graph.pipeline import review as review_mod

    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    # 追加一段没有编号的导言，确保 chunk 层在 review 崩溃前已产生
    # NO_STANDARD_CODE 记录
    source.write_text(SOURCE + "\n这一段是导言，没有编号。\n", encoding="utf-8")
    out = tmp_path / "out"

    def boom(*args, **kwargs):
        raise RuntimeError("模拟 review 层崩溃")

    monkeypatch.setattr(review_mod, "review_drafts", boom)

    with pytest.raises(RuntimeError):
        _run(engine, source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    dropped_path = out / "dropped.json"
    assert dropped_path.exists(), f"{engine}: 崩溃后 dropped.json 缺失"
    drops = json.loads(dropped_path.read_text(encoding="utf-8"))
    assert any(d["reason"] == "NO_STANDARD_CODE" for d in drops)


@ENGINES
def test_all_drafts_rejected_by_review_yields_empty_generation_error(tmp_path, engine):
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

    findings = _run(
        engine, source.parent, out, deps, model_id="fake", curriculum="cn-moe-math-2022"
    )

    assert has_errors(findings)
    empty_findings = [f for f in findings if f.code == "EMPTY_GENERATION"]
    assert len(empty_findings) == 1
    finding = empty_findings[0]
    assert finding.severity is Severity.ERROR
    # context 要带够诊断信息，让人不用重新跑一遍就能定位是哪一层归零的
    for key in ("chunks", "drafts", "deduped", "reviewed"):
        assert key in finding.context


@ENGINES
def test_edge_prerequisite_rejected_by_review_is_recorded_not_silently_dropped(tmp_path, engine):
    """Critical C2 复现：前置节点被 review 淘汰时，04-edges.json 里有该提议边，
    最终 dependencies 却是 0，此前 dropped.json 里只有那个节点的
    REVIEW_REJECTED，没有任何一条说明"那条边"消失了。编排层预过滤必须
    显式记账，不能靠 dict comprehension 静默丢。"""
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    deps = _fake_deps()
    # 只否决前置节点（3.1.1，被抽成"认识100以内的数"），目标节点保留
    deps.fidelity_judges = [
        lambda d: Vote(
            reviewer="fake",
            approved=d.content.name != "认识100以内的数",
            reason="仅否决前置",
        )
    ]

    _run(engine, source.parent, out, deps, model_id="fake", curriculum="c")

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert graph["dependencies"] == []

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    edge_drops = [d for d in drops if d["reason"] == "EDGE_PREREQUISITE_REJECTED"]
    assert len(edge_drops) == 1
    assert "<-" in edge_drops[0]["ref"]


@ENGINES
def test_edge_target_rejected_by_review_is_recorded_not_silently_dropped(tmp_path, engine):
    """两处过滤的另一处：目标节点整个被淘汰时，指向它的整组边也要留痕，
    而不是随着 dict comprehension 的 `if target in kept_ids` 悄悄消失。"""
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    deps = _fake_deps()
    # 只否决边的目标节点（3.1.2，被抽成"100以内加减法"），前置节点保留
    deps.fidelity_judges = [
        lambda d: Vote(
            reviewer="fake",
            approved=d.content.name != "100以内加减法",
            reason="仅否决目标",
        )
    ]

    _run(engine, source.parent, out, deps, model_id="fake", curriculum="c")

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert graph["dependencies"] == []

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    edge_drops = [d for d in drops if d["reason"] == "EDGE_TARGET_REJECTED"]
    assert len(edge_drops) == 1
    assert "<-" in edge_drops[0]["ref"]


@ENGINES
def test_duplicate_proposed_edge_is_collapsed_and_recorded(tmp_path, engine):
    """待裁决 #5 复现：同一 (target, prereq) 给出 hard + soft 两条边，此前两条
    都会进 graph.json 的 dependencies，run_all 零 finding —— 编排层现在应在
    调用 assemble 之前去重（hard 胜 soft）并记一条 DUPLICATE_EDGE。"""
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    deps = _fake_deps()

    def proposer(target, candidates):
        prereq = candidates[0].draft_id
        return ProposedEdgeBatch(
            edges=[
                ProposedEdge(prerequisite_draft_id=prereq, strength="soft", reason="有帮助"),
                ProposedEdge(prerequisite_draft_id=prereq, strength="hard", reason="其实必须"),
            ]
        )

    deps.edge_proposer = proposer

    _run(engine, source.parent, out, deps, model_id="fake", curriculum="c")

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert len(graph["dependencies"]) == 1
    assert graph["dependencies"][0]["strength"] == "hard"

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    dup_drops = [d for d in drops if d["reason"] == "DUPLICATE_EDGE"]
    assert len(dup_drops) == 1


# --- 修复 6（审查者头号建议）：把"没有静默跳过"这条第一原则写成
# 跨层不变量测试，而不只是口号。
#
# 守恒式的准确形式（见下方注释里的推导）：
#
#   节点：len(02-drafts.json) == topics 数 + merges 数 + 节点相关 DropRecord 数
#   边：  len(04-edges.json)  == dependencies 数 + 边相关 DropRecord 数
#
# 节点侧的关键点：merge 会让 draft 数减少，但那不是"丢弃"——merges.json
# 单独记录，必须算进守恒式里，否则这条断言永远对不上（这正是简报里的
# 提醒）。SAME_TOPIC_JUDGE_FAILED 也不算节点丢弃：判定失败时两个 draft
# 都原样保留、只是没能合并，不会导致任何一个 draft 消失。
#
# 边侧的关键点："REVIEW_REJECTED" 这个原因码在 review.py 里被两处复用：
# review_drafts 淘汰节点时 ref 是裸 draft_id，review_edges 淘汰边时 ref 是
# "target<-prereq"。用 ref 是否含 "<-" 来判断一条 REVIEW_REJECTED 记录
# 到底该计入节点侧还是边侧 —— 这也是本次顺带修的一处 ref 格式（原来边侧
# 的 REVIEW_REJECTED.ref 只是裸 target_id，和节点侧撞在一起分不清）。
#
# 后续修的假阳性（边守恒公式本身的缺陷，而非场景覆盖不足）：
# UNKNOWN_PREREQUISITE / NON_CANDIDATE_PREREQUISITE 产生于
# edges.py::propose_all **内部**——被它们拒掉的边从未进入 propose_all
# 的返回值，因此也从未被写进 04-edges.json。而上面这条边守恒式是
# "len(04-edges.json) == dependencies 数 + 边相关 DropRecord 数"，
# 分母（04-edges.json）本就已经不含这两类边，若还把它们的 DropRecord
# 计进"边相关 DropRecord 数"，等于对同一条边减了两次：一次是它本来就
# 没出现在分母里，一次是又被当成"从分母掉到 drops 里的那一个"减掉——
# 公式永远对不上，且是在系统行为完全正确时对不上（真实复现：构造一个
# 触发 UNKNOWN_PREREQUISITE 的场景，len(04-edges.json)=0，
# dependencies=0，若把这条 DropRecord 计入边相关 drops 则
# len(edge_drops)=1，0 == 0 + 1 不成立）。
#
# **我的判断**：不选"两级守恒式"（模型原始提议边总数 → propose_all 产出
# → 最终 dependencies 各自守恒）——因为本测试文件只能拿到 propose_all
# 的返回值和落盘产物，拿不到"模型这次调用原始吐出了多少条边"这个分母
# （fake proposer 的返回值在 propose_all 内部就被消费掉，run_pipeline
# 没有把它透传出来），要做两级式就得改生产代码只为了让测试能读到一个
# 中间量，不值得。所以选择：把这两个原因码排除在本条跨层不变量之外，
# 它们的"没有静默跳过"由 propose_all 自己的单元测试覆盖——
# tests/pipeline/test_edges.py 的
# test_edges_pointing_at_unknown_drafts_are_dropped（UNKNOWN_PREREQUISITE）
# 与 test_self_referencing_edge_is_dropped /
# test_edge_pointing_outside_the_candidate_pool_is_dropped
# （NON_CANDIDATE_PREREQUISITE）逐条断言：被拒的边不进 edges 返回值、
# 且必留一条对应原因码的 DropRecord——propose_all 内部本身就是守恒的，
# 不需要也无法在跨层这一层重复对账。
# 下面 test_unknown_prerequisite_is_excluded_from_edge_conservation_and_recorded
# 补一层管道级验证：主动触发 UNKNOWN_PREREQUISITE，确认（1）排除之后
# 边守恒式在这种场景下不再误报（2）该 DropRecord 没有因为被移出公式
# 就没人管——dropped.json 里确实记了一条。

_NODE_ONLY_REASONS = {"SAME_NAME_DIFFERENT_TOPIC", "FIDELITY_JUDGE_FAILED", "NAME_JUDGE_FAILED"}
_EDGE_ONLY_REASONS = {
    "EDGE_TARGET_REJECTED",
    "EDGE_PREREQUISITE_REJECTED",
    "EDGE_JUDGE_FAILED",
    "UNKNOWN_REVIEW_TARGET",
    "DUPLICATE_EDGE",
}


def _is_node_drop(record: dict) -> bool:
    if record["reason"] in _NODE_ONLY_REASONS:
        return True
    return record["reason"] == "REVIEW_REJECTED" and "<-" not in record["ref"]


def _is_edge_drop(record: dict) -> bool:
    if record["reason"] in _EDGE_ONLY_REASONS:
        return True
    return record["reason"] == "REVIEW_REJECTED" and "<-" in record["ref"]


@ENGINES
def test_conservation_invariant_holds_across_a_mixed_scenario(tmp_path, engine):
    """第一原则的可执行断言：没有静默跳过 == 每条没能进入产出的输入都留下了
    带原因码的 DropRecord，多到能与产出对上账。

    混合场景（不是全 happy path）：
    - 一段没有条目编号，chunk 层丢弃（NO_STANDARD_CODE，不计入下面两条守恒式，
      它发生在"draft"这个计量单位诞生之前）
    - 两个 draft（分数的意义 / 分数的意义（换种说法）3.1.3、3.1.4）会在 dedupe
      合并成一个 —— 验证 merges 被正确算进节点守恒式
    - 两个 draft 被 fidelity 判定否决（3.1.1 的"读写100以内的数"、3.1.5 的
      "小数的意义"）—— 前者是某条边的前置，后者是某条边的目标，
      分别触发 EDGE_PREREQUISITE_REJECTED 与 EDGE_TARGET_REJECTED
      （对应修复 1 里说的"两处过滤"）
    - "分数的意义"目标下给出一条重复边（同一前置，soft + hard）——
      触发修复 5 的 DUPLICATE_EDGE
    """
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "3.1.1 能认识并读写 100 以内的数。\n\n"
        "3.1.2 能计算 100 以内的加减法。\n\n"
        "3.1.3 能理解分数的意义。\n\n"
        "3.1.4 能理解分数的意义（换一种说法）。\n\n"
        "3.1.5 能理解小数的意义。\n\n"
        "这一段是导言，没有编号。\n",
        encoding="utf-8",
    )
    out = tmp_path / "out"

    names_by_code = {
        "3.1.1": "读写100以内的数",
        "3.1.2": "100以内加减法",
        "3.1.3": "分数的意义",
        "3.1.4": "分数的意义（换种说法）",
        "3.1.5": "小数的意义",
    }
    grades_by_code = {"3.1.1": 1, "3.1.2": 2, "3.1.3": 3, "3.1.4": 3, "3.1.5": 3}

    def extractor(chunk):
        name = names_by_code[chunk.standard_code]
        # D3（3.1.3）给多一条证据，保证 dedupe 的 _better_base 稳定选它做
        # 合并基底 —— 这样下面的边/审核逻辑只需按 content.name 定位，
        # 不用关心合并后具体存活的是哪个 draft_id。
        evidence = ["证据一", "证据二"] if chunk.standard_code == "3.1.3" else None
        return DraftBatch(
            drafts=[_content(name, grades_by_code[chunk.standard_code], chunk.text, evidence)]
        )

    def same_topic(a, b):
        if a.content.name.startswith("分数的意义") and b.content.name.startswith("分数的意义"):
            return SameTopicVerdict(same=True, reason="措辞不同，同一个知识点")
        return SameTopicVerdict(same=False, reason="不同")

    def proposer(target, candidates):
        by_name = {c.content.name: c for c in candidates}
        name = target.content.name
        if name == "100以内加减法":
            prereq = by_name["读写100以内的数"]
            return ProposedEdgeBatch(
                edges=[
                    ProposedEdge(
                        prerequisite_draft_id=prereq.draft_id, strength="hard", reason="先会读写数"
                    )
                ]
            )
        if name == "分数的意义":
            prereq = by_name["100以内加减法"]
            return ProposedEdgeBatch(
                edges=[
                    ProposedEdge(
                        prerequisite_draft_id=prereq.draft_id, strength="soft", reason="有帮助（弱）"
                    ),
                    ProposedEdge(
                        prerequisite_draft_id=prereq.draft_id, strength="hard", reason="其实必须（重复边）"
                    ),
                ]
            )
        if name == "小数的意义":
            prereq = by_name["100以内加减法"]
            return ProposedEdgeBatch(
                edges=[
                    ProposedEdge(
                        prerequisite_draft_id=prereq.draft_id, strength="hard", reason="随便先修"
                    )
                ]
            )
        return ProposedEdgeBatch(edges=[])

    def fidelity(draft):
        rejected_names = {"读写100以内的数", "小数的意义"}
        return Vote(
            reviewer="fake", approved=draft.content.name not in rejected_names, reason="仅否决两个"
        )

    deps = PipelineDeps(
        extractor=extractor,
        same_topic_judge=same_topic,
        edge_proposer=proposer,
        fidelity_judges=[fidelity],
        name_judges=[lambda name, description: Verdict(judgment="consistent")],
        edge_judges=[lambda t, e: Vote(reviewer="fake", approved=True, reason="ok")],
    )

    _run(engine, source.parent, out, deps, model_id="fake", curriculum="c")

    drafts = json.loads((out / "02-drafts.json").read_text(encoding="utf-8"))
    merges = json.loads((out / "merges.json").read_text(encoding="utf-8"))
    proposed_edges = json.loads((out / "04-edges.json").read_text(encoding="utf-8"))
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))

    # 场景确实如预期地混合、而不是意外全 happy path 蒙混过关
    assert any(d["reason"] == "NO_STANDARD_CODE" for d in drops)
    assert len(merges) == 1
    node_drops = [d for d in drops if _is_node_drop(d)]
    edge_drops = [d for d in drops if _is_edge_drop(d)]
    assert {d["reason"] for d in node_drops} == {"REVIEW_REJECTED"}
    assert len(node_drops) == 2  # 读写100以内的数、小数的意义
    assert {d["reason"] for d in edge_drops} == {
        "EDGE_PREREQUISITE_REJECTED",
        "EDGE_TARGET_REJECTED",
        "DUPLICATE_EDGE",
    }

    # 节点守恒：draft 总数 == 最终 topics + merges + 节点相关 DropRecord
    assert len(drafts) == len(graph["topics"]) + len(merges) + len(node_drops)

    # 边守恒：提议边总数（04-edges.json）== 最终 dependencies + 边相关 DropRecord
    assert len(proposed_edges) == len(graph["dependencies"]) + len(edge_drops)


@ENGINES
def test_unknown_prerequisite_is_excluded_from_edge_conservation_and_recorded(tmp_path, engine):
    """主动触发 UNKNOWN_PREREQUISITE，验证上面那条排除决定站得住脚。

    fake edge proposer 引用一个压根不存在的 prerequisite_draft_id ——
    这条边在 propose_all 内部就被拒，从未进入 04-edges.json（复现审查者
    的实证：len(04-edges.json)=0）。断言两件事：
    1. 边守恒式（以 04-edges.json 为基准）在这种场景下仍然成立，不误报；
    2. 这条 DropRecord 没有因为被移出守恒式就没人管 —— dropped.json 里
       确实记了一条 UNKNOWN_PREREQUISITE。
    """
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    deps = _fake_deps()

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[
                ProposedEdge(
                    prerequisite_draft_id="ghost-draft-id-does-not-exist",
                    strength="hard",
                    reason="编出来的前置",
                )
            ]
        )

    deps.edge_proposer = proposer

    _run(engine, source.parent, out, deps, model_id="fake", curriculum="c")

    proposed_edges = json.loads((out / "04-edges.json").read_text(encoding="utf-8"))
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))

    # 复现审查者的实证：那条被拒的边确实没进 04-edges.json
    assert proposed_edges == []
    assert graph["dependencies"] == []

    # 断言 2：DropRecord 确实被记录，没有因为移出守恒式就没人管
    unknown_drops = [d for d in drops if d["reason"] == "UNKNOWN_PREREQUISITE"]
    assert len(unknown_drops) == 1
    assert "ghost-draft-id-does-not-exist" in unknown_drops[0]["detail"]

    # 断言 1：边守恒式仍然成立 —— UNKNOWN_PREREQUISITE 已不在 _EDGE_ONLY_REASONS
    # 里，不会被误计入边相关 drops，公式不会像修复前那样假阳性地 FAIL。
    edge_drops = [d for d in drops if _is_edge_drop(d)]
    assert len(edge_drops) == 0
    assert len(proposed_edges) == len(graph["dependencies"]) + len(edge_drops)


@ENGINES
def test_normal_generation_does_not_emit_empty_generation_finding(tmp_path, engine):
    """防误报：正常产出时不该背上 EMPTY_GENERATION。"""
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    findings = _run(
        engine, source.parent, out, _fake_deps(), model_id="fake", curriculum="cn-moe-math-2022"
    )

    assert not any(f.code == "EMPTY_GENERATION" for f in findings)


@ENGINES
def test_pipeline_reports_orphans_created_by_review(tmp_path, engine):
    """端到端：基础节点被淘汰后，后继要被标记为孤儿。

    本文件的 `_fake_deps` 的 extractor 是按 `chunk.standard_code` 定死名字的
    （3.1.1 -> "认识100以内的数"，其余 -> "100以内加减法"），源文件里的
    "甲条目"/"乙条目" 文本本身不影响抽出的 draft 名字，只要编号是 3.1.1/3.1.2
    即可触发这两个固定名字。brief 原始草稿写的是否决 "甲知识点"，但那个
    名字在这份 fixture 下不对应任何真实 draft、永远不会被否决 —— 我按实际
    会被抽出的 draft 名字改成否决 "认识100以内的数"（它是 "100以内加减法"
    唯一的前置），行为要求（基础节点淘汰后，后继变孤儿）不变。
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲条目。\n\n3.1.2 乙条目。\n", encoding="utf-8")
    out = tmp_path / "out"

    _run(engine, source, out, _deps_rejecting("认识100以内的数"), model_id="fake", curriculum="c")

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    assert any(d["reason"] == "ORPHANED_BY_REJECTION" for d in drops)


@ENGINES
def test_pipeline_does_not_claim_consistency_was_skipped(tmp_path, engine):
    """review 层明明跑过 name judge，最终报告却说『已跳过』—— 这条留痕机制
    自己出的岔子，比不留痕更误导人。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲条目。\n", encoding="utf-8")

    findings = _run(engine, source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c")

    assert not any(f.code == "CONSISTENCY_SKIPPED" for f in findings)
