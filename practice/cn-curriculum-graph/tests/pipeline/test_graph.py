"""LangGraph 编排的对等性测试。

硬标准：与手写版行为一致。所以这里只测"结构上是不是六个 Node、
state 累加语义对不对"，行为一致性由 test_run.py 的参数化端到端测试保证。
"""

import json
import operator
from typing import Annotated, get_type_hints

import pytest

from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import graph as graph_mod
from cn_curriculum_graph.pipeline.faults import FaultSpec, wrap_deps
from cn_curriculum_graph.pipeline.graph import PipelineState, build_graph, retry_on, run_pipeline_lg
from cn_curriculum_graph.pipeline.models import ProposedEdge, ProposedEdgeBatch
from .test_run import SOURCE, _fake_deps


def test_state_accumulates_drops_across_nodes():
    """drops 是唯一带 reducer 的字段 —— 手写版五处显式 append_drops，
    这里声明一次，累加语义成了类型的一部分。"""
    hints = get_type_hints(PipelineState, include_extras=True)
    assert hints["drops"].__metadata__ == (operator.add,)


def test_other_fields_are_overwrite_not_accumulate():
    """除 drops 外都是覆盖语义，别不小心给 chunks 也加了 reducer。"""
    hints = get_type_hints(PipelineState, include_extras=True)
    assert not hasattr(hints["chunks"], "__metadata__")


def test_graph_has_one_node_per_pipeline_layer():
    """六层一一对应。多一个少一个都说明 Node 里塞了不该塞的东西。"""
    compiled = build_graph().compile()
    nodes = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

    assert nodes == {"chunk", "extract", "dedupe", "edges", "review", "assemble"}


def test_graph_is_linear():
    compiled = build_graph().compile()
    edges = {(e.source, e.target) for e in compiled.get_graph().edges}

    assert ("chunk", "extract") in edges
    assert ("extract", "dedupe") in edges
    assert ("dedupe", "edges") in edges
    assert ("edges", "review") in edges
    assert ("review", "assemble") in edges


class _Rate(Exception):
    pass


def test_retry_on_skips_programming_errors():
    """程序 bug 不该被重试 —— 重试三次只会把同一个 bug 犯三遍。
    与手写版收窄 except 的策略同源。"""
    assert retry_on(_Rate("429")) is True
    for exc in (AttributeError(), TypeError(), NameError(), KeyError()):
        assert retry_on(exc) is False


def test_transient_failure_is_retried_and_recovers(tmp_path, monkeypatch):
    """extract 层第 1 次调用整体抛一次瞬时错误，RetryPolicy 应当在 Node
    粒度自愈，管道跑完。

    **与 brief 原始草稿的差异（我的判断，已用实测核实）**：brief 原稿是在
    `PipelineDeps.extractor` 上挂 `FaultSpec` 控制第 N 次调用失败。实测
    发现这样注入永远测不出 Node 级重试 —— `extract_mod.extract_all` 对每个
    chunk 各自 `try/except`，非 `PROGRAMMING_ERRORS` 会被就地转成
    `DropRecord` 并 `continue`，从不冒泡出 `extract_all` 本身（这是"单条
    失败不中断整批"这条既有设计的直接后果，见 extract.py 的实现）。
    照抄 brief 原稿实测结果：2 chunk 场景下 `counter.counts["extractor"]`
    始终等于 2（从未触发过重试），与 brief 断言的 3 不符——完整报错见
    task-5-report.md。

    Node 级 RetryPolicy 只对"整层调用彻底失败"（即 `extract_all` 这次
    调用本身抛出，而不是它内部某个 chunk 抛出后被吞掉）才有意义，所以这里
    改为 monkeypatch `extract_mod.extract_all`（六层纯函数本身不改一行，
    只是测试里换了一层壳），与 test_run.py 的
    test_dropped_json_survives_a_crash_mid_pipeline 同一手法
    （monkeypatch 六层函数模拟节点级失败）。
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n\n3.1.2 乙。\n", encoding="utf-8")

    real_extract_all = extract_mod.extract_all
    calls = {"n": 0}

    def flaky_extract_all(chunks, extractor):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _Rate("429")
        return real_extract_all(chunks, extractor)

    monkeypatch.setattr(graph_mod.extract_mod, "extract_all", flaky_extract_all)

    findings = run_pipeline_lg(
        source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c"
    )

    assert (tmp_path / "out" / "graph.json").exists()
    # 第 1 次调用失败，RetryPolicy 重试后第 2 次成功——整层重新跑一遍
    assert calls["n"] == 2


def test_checkpoint_resumes_instead_of_rerunning_completed_nodes(tmp_path, monkeypatch):
    """不可恢复的失败 + checkpointer：第二次跑不应重跑已完成的 chunk/extract。

    同上一条测试的理由：`edges_mod.propose_all` 同样对每个 target 各自
    `try/except`，挂在 `edge_proposer` 上的 `FaultSpec` 只会被转成
    `DropRecord`，从不让 `propose_all` 本身抛出、也就从不会让
    `run_pipeline_lg` 真正报错——照抄 brief 原稿会在
    `assert first_extract_calls > 0` 之前的 `pytest.raises(Exception)` 处
    静默通过（因为根本没有异常被抛出，`pytest.raises` 捕获失败会在这里
    报 `Failed: DID NOT RAISE`），完整报错见 task-5-report.md。这里同样
    改为 monkeypatch `edges_mod.propose_all` 制造一次"整层调用不可恢复的
    失败"（哪怕 RetryPolicy 重试 3 次也救不回来），第二次用
    `monkeypatch.undo()` 撤销故障后续跑应当成功。
    extractor 调用计数这部分不涉及异常传播、只是普通计数，
    `wrap_deps`/`FaultSpec` 完全适用，保留原方案。
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n\n3.1.2 乙。\n", encoding="utf-8")
    db = tmp_path / "cp.sqlite"

    def boom(drafts, proposer):
        raise _Rate("模拟 edges 层持续故障，重试 3 次也救不回来")

    monkeypatch.setattr(graph_mod.edges_mod, "propose_all", boom)

    boom_deps, boom_counter = wrap_deps(_fake_deps(), [])
    with pytest.raises(Exception):
        run_pipeline_lg(
            source, tmp_path / "out", boom_deps, model_id="fake",
            curriculum="c", checkpoint_db=db, thread_id="t1",
        )
    first_extract_calls = boom_counter.counts["extractor"]
    assert first_extract_calls > 0

    # 撤销故障，第二次同一 thread_id 续跑：extract 不该被重新调用
    monkeypatch.undo()
    good_deps, good_counter = wrap_deps(_fake_deps(), [])
    run_pipeline_lg(
        source, tmp_path / "out", good_deps, model_id="fake",
        curriculum="c", checkpoint_db=db, thread_id="t1",
    )

    assert good_counter.counts["extractor"] == 0
    assert (tmp_path / "out" / "graph.json").exists()


def test_assemble_retry_via_checkpoint_does_not_duplicate_drops(tmp_path, monkeypatch):
    """锁死本任务描述的交互风险：node_assemble 若在 assemble_mod.assemble/
    run_all 之前就把 dup_drops 落盘，assemble 这一步一旦失败（该 Node 没有
    retry_policy，只能靠重新调用 run_pipeline_lg 走 checkpoint 续跑恢复），
    续跑会让 node_assemble 整个函数体重新执行一遍——dup_drops 被重新算出
    并再 append 一次，dropped.json 里每条 DUPLICATE_EDGE 都会变成两份。

    这条测试在修复前（把 io.append_drops(dup_drops) 挪到 assemble 之后）
    应当能复现重复记录，修复后应变为 1 条。
    """
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    db = tmp_path / "cp.sqlite"

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

    real_assemble = assemble_mod.assemble
    calls = {"n": 0}

    def flaky_assemble(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("模拟 assemble 层崩溃（assemble 无 retry_policy，不会自动重试）")
        return real_assemble(*args, **kwargs)

    monkeypatch.setattr(graph_mod.assemble_mod, "assemble", flaky_assemble)

    with pytest.raises(RuntimeError):
        run_pipeline_lg(
            source.parent, tmp_path / "out", deps, model_id="fake",
            curriculum="c", checkpoint_db=db, thread_id="t-assemble",
        )

    # 续跑：assemble 这次不再崩，应当成功产出 graph.json
    run_pipeline_lg(
        source.parent, tmp_path / "out", deps, model_id="fake",
        curriculum="c", checkpoint_db=db, thread_id="t-assemble",
    )

    drops = json.loads((tmp_path / "out" / "dropped.json").read_text(encoding="utf-8"))
    dup_drops = [d for d in drops if d["reason"] == "DUPLICATE_EDGE"]
    assert len(dup_drops) == 1, f"dropped.json 里 DUPLICATE_EDGE 不应重复，实际：{dup_drops}"
