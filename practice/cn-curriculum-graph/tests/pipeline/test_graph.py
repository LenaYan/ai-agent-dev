"""LangGraph 编排的对等性测试。

硬标准：与手写版行为一致。所以这里只测"结构上是不是六个 Node、
state 累加语义对不对"，行为一致性由 test_run.py 的参数化端到端测试保证。
"""

import json
import operator
import time
from datetime import timedelta
from typing import Annotated, get_type_hints

import pytest
from langgraph.errors import NodeTimeoutError
from langgraph.types import RetryPolicy

from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import graph as graph_mod
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.faults import wrap_deps
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


def test_node_timeout_actually_fires_for_blocking_node_body(tmp_path, monkeypatch):
    """C1 回归锁定：`NODE_TIMEOUT` 必须真的能打断一个 Node，而不只是挂在
    `add_node(..., timeout=...)` 上却从不生效。

    四个 LLM Node 虽是 `async def`，函数体过去是直接同步调用六层函数
    （底下是阻塞的 HTTP 请求），从不 `await` 任何东西——这会把事件循环
    整个占住，`langgraph.pregel._retry._arun_with_timeout` 里和 Node 赛跑的
    看门狗 task 拿不到调度机会，`timeout` 形同虚设。

    复现方式（与审查者的手动实测一致）：把 extract 层换成一个纯同步
    `time.sleep(3)`（无 await），`NODE_TIMEOUT` 调到 1 秒。
    - 修复前：整个事件循环被 sleep 占满，看门狗排不上号，管道正常跑完，
      不抛任何异常 —— 这条测试应当 FAIL（`DID NOT RAISE`）。
    - 修复后（Node 体内改用 `await asyncio.to_thread(...)`）：sleep 被扔进
      后台线程，事件循环空出来，看门狗如期在 1 秒后判定超时，
      `NodeTimeoutError` 真实抛出。
    """
    monkeypatch.setattr(graph_mod, "NODE_TIMEOUT", timedelta(seconds=1))
    # 关掉重试：这条测试只想验证"超时会不会被触发"，不想被"重试 3 次、
    # 每次都要等阻塞体跑完/被放弃"的额外耗时和噪音干扰断言。
    monkeypatch.setattr(graph_mod, "RETRY_POLICY", RetryPolicy(max_attempts=1, retry_on=retry_on))

    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n", encoding="utf-8")

    real_extract_all = extract_mod.extract_all

    def blocking_extract_all(chunks, extractor):
        # 刻意纯同步阻塞、函数体里没有任何 await —— 这正是修复前四个
        # Node 的真实形状（六层函数底下是阻塞的 anthropic SDK 调用）。
        # 注意：这里必须调用先保存下来的 real_extract_all，不能调用
        # `extract_mod.extract_all` —— 那个名字这一行之后会被 monkeypatch
        # 指向 blocking_extract_all 自己，调它等于递归调用自己、永不返回。
        time.sleep(3)
        return real_extract_all(chunks, extractor)

    monkeypatch.setattr(graph_mod.extract_mod, "extract_all", blocking_extract_all)

    with pytest.raises(NodeTimeoutError) as exc_info:
        run_pipeline_lg(
            source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c"
        )

    assert "run timeout of 1.000s" in str(exc_info.value)


def test_node_timeout_does_not_provide_a_wall_clock_bound(tmp_path, monkeypatch):
    """第一部分遗留项回归：NODE_TIMEOUT 的文档曾经写"这意味着'流水线不再
    永远挂着'"——这句话是错的，实测钉住。

    根因（CPython `asyncio.runners.Runner.close()`）：`asyncio.to_thread`
    用的是 default executor，收尾阶段会
    `run_until_complete(loop.shutdown_default_executor(THREAD_JOIN_TIMEOUT))`
    （`THREAD_JOIN_TIMEOUT = 300`），也就是说会去 join 那条杀不掉的后台线程，
    最多等 300 秒——`timeout` 参数改变的只是"返回结果"（拿到异常而非正常
    返回值），不是墙钟上界。

    复现：sleep=3s（不能杀掉）、NODE_TIMEOUT=1s。`NodeTimeoutError` 如期在
    约 1s 抛出，但 `run_pipeline_lg` 实际耗时接近 3s（而非 1s）才真正返回——
    墙钟被 THREAD_JOIN_TIMEOUT 机制拖到了线程自然结束。
    """
    monkeypatch.setattr(graph_mod, "NODE_TIMEOUT", timedelta(seconds=1))
    monkeypatch.setattr(graph_mod, "RETRY_POLICY", RetryPolicy(max_attempts=1, retry_on=retry_on))

    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n", encoding="utf-8")

    real_extract_all = extract_mod.extract_all
    sleep_seconds = 3

    def blocking_extract_all(chunks, extractor):
        time.sleep(sleep_seconds)
        return real_extract_all(chunks, extractor)

    monkeypatch.setattr(graph_mod.extract_mod, "extract_all", blocking_extract_all)

    started = time.monotonic()
    with pytest.raises(NodeTimeoutError):
        run_pipeline_lg(
            source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c"
        )
    elapsed = time.monotonic() - started

    # 核心断言：run_pipeline_lg 的实际墙钟耗时接近 sleep_seconds（后台线程
    # 自然结束所需时间），而非接近 NODE_TIMEOUT（1s）——如果 timeout 真的
    # 提供了墙钟上界，elapsed 应该远小于 sleep_seconds，这条断言就会 FAIL。
    assert elapsed >= sleep_seconds * 0.9, (
        f"预期 run_pipeline_lg 仍阻塞到后台线程自然结束（约 {sleep_seconds}s），"
        f"实际只用了 {elapsed:.2f}s 就返回了——如果这条断言意外 FAIL，说明 "
        "NODE_TIMEOUT 确实提供了墙钟上界，本条注释与 graph.py 里的文档都要反过来改"
    )


def test_programming_errors_is_imported_from_models_not_duplicated():
    """S4：graph.py 曾经维护一份与 pipeline/models.py 字面相同的
    PROGRAMMING_ERRORS 拷贝——今天内容相同不代表以后也同步：往权威定义
    （models.py，四个模块都 import 它）里加一个类型，不会魔法般同步到
    这份拷贝。用 `is` 而非 `==` 断言：内容相等的两个独立元组恒为
    `False is`，只有『同一个对象』才会是 `True`，这正是本条要锁的东西
    ——graph.py 不该再自己 `= (...)` 出一份，而应该 import 权威定义。"""
    from cn_curriculum_graph.pipeline import models as models_mod

    assert graph_mod.PROGRAMMING_ERRORS is models_mod.PROGRAMMING_ERRORS


def test_retry_on_excludes_value_error_to_avoid_repeating_config_mistakes(tmp_path, monkeypatch):
    """S3：review.py 里三处空 judges 哨兵抛的是 ValueError——确定性的配置
    错误（比如忘了传 judges）。RETRY_POLICY 的注释自己写着"程序 bug 不该
    被重试——重试三次只会把同一个 bug 犯三遍"，但收窄 except 的
    PROGRAMMING_ERRORS 里没有 ValueError，导致这类配置错误被 Node 级
    RetryPolicy 犯了三遍。这里用 monkeypatch 包一层计数器套在
    review_mod.review_drafts 上，验证 ValueError 触发时该函数只被调用
    1 次（不重试），而不是 max_attempts=3 次。
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n", encoding="utf-8")

    real_review_drafts = review_mod.review_drafts
    calls = {"n": 0}

    def counting_review_drafts(*args, **kwargs):
        calls["n"] += 1
        return real_review_drafts(*args, **kwargs)

    monkeypatch.setattr(graph_mod.review_mod, "review_drafts", counting_review_drafts)

    deps = _fake_deps()
    deps.fidelity_judges = []  # 触发 review_drafts 顶部的空 judges ValueError 哨兵

    with pytest.raises(ValueError):
        run_pipeline_lg(source, tmp_path / "out", deps, model_id="fake", curriculum="c")

    assert calls["n"] == 1, (
        f"ValueError（配置错误）不该被 Node 级 RetryPolicy 重试，"
        f"实际 review_drafts 被调用了 {calls['n']} 次"
    )


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


def test_checkpoint_db_parent_dir_is_created_if_missing(tmp_path):
    """Minor 项：`--checkpoint` 指向一个父目录不存在的路径时，不应该让
    sqlite 抛裸 `sqlite3.OperationalError`（"unable to open database
    file"）——那条报错不指名道姓是"目录不存在"，排查体验很差。`out_dir`
    早就有 `mkdir(parents=True, exist_ok=True)`，`checkpoint_db` 的父目录
    应该享受同等待遇。
    """
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n", encoding="utf-8")

    # 父目录（nested/deeper）故意不存在
    db = tmp_path / "nested" / "deeper" / "cp.sqlite"
    assert not db.parent.exists()

    run_pipeline_lg(
        source, tmp_path / "out", _fake_deps(), model_id="fake",
        curriculum="c", checkpoint_db=db, thread_id="t-mkdir",
    )

    assert db.exists()
    assert (tmp_path / "out" / "graph.json").exists()


def test_resume_with_different_source_or_out_raises_instead_of_silently_reusing_old_dirs(
    tmp_path, monkeypatch
):
    """S1 Critical：resume 时（同一 thread_id、同一 checkpoint db）若这次调用的
    source_dir/out_dir 与 checkpoint 里存的不一致，必须 raise，绝不能静默沿用
    checkpoint 里的旧值——否则实验 B 传的 srcB/outB 会被整个丢弃，产物全部
    落进实验 A 的 outA，outB 里什么都没有却退出码 0。

    先用 srcA/outA 制造一个"未完成的 checkpoint"（edges 层崩溃），
    再用完全不同的 srcB/outB、同一 thread_id/checkpoint db 去 resume。
    """
    source_a = tmp_path / "srcA"
    source_a.mkdir()
    (source_a / "m.md").write_text("3.1.1 甲。\n\n3.1.2 乙。\n", encoding="utf-8")
    out_a = tmp_path / "outA"
    db = tmp_path / "cp.sqlite"

    def boom(drafts, proposer):
        raise _Rate("模拟 edges 层崩溃，制造一个未完成的 checkpoint")

    monkeypatch.setattr(graph_mod.edges_mod, "propose_all", boom)

    with pytest.raises(Exception):
        run_pipeline_lg(
            source_a, out_a, _fake_deps(), model_id="fake",
            curriculum="c", checkpoint_db=db, thread_id="shared",
        )

    monkeypatch.undo()

    source_b = tmp_path / "srcB"
    source_b.mkdir()
    (source_b / "m.md").write_text("3.1.1 丙。\n\n3.1.2 丁。\n", encoding="utf-8")
    out_b = tmp_path / "outB"

    with pytest.raises(ValueError, match="source_dir"):
        run_pipeline_lg(
            source_b, out_b, _fake_deps(), model_id="fake",
            curriculum="c", checkpoint_db=db, thread_id="shared",
        )

    # 静默沿用旧目录的坏行为不该再发生：outB 不该被写入任何产物，
    # outA 里的产物也不该被这次用 srcB 发起的调用污染
    assert not (out_b / "graph.json").exists()
    assert "丙" not in (out_a / "01-chunks.json").read_text(encoding="utf-8")


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
