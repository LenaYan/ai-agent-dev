"""B 阶段：条目级扇出。

⚠️ 这已不是"同一需求的两种实现" —— 架构变了。它回答的是另一个问题
（框架解锁了什么），笔记里必须与第一章分开读。

**不测什么**：不接入 test_run.py 那 13 条两引擎对等的参数化端到端测试——
B 阶段的架构已经偏离"与手写版对等"这条硬标准，硬凑对等测试等于假装
两者仍在回答同一个问题。这里只测 B 阶段自己的结构与行为。
"""

from __future__ import annotations

import json
import os
import threading
import time

import pytest
from langgraph.types import RetryPolicy

from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import graph_fanout as graph_fanout_mod
from cn_curriculum_graph.pipeline.graph import retry_on
from cn_curriculum_graph.pipeline.graph_fanout import build_fanout_graph, run_pipeline_fanout
from cn_curriculum_graph.pipeline.models import DraftBatch, Vote
from cn_curriculum_graph.pipeline.review import FidelityVerdict
from cn_curriculum_graph.runner import has_errors

from .test_run import SOURCE, _content, _fake_deps


def test_fanout_graph_has_a_per_chunk_extract_node():
    compiled = build_fanout_graph().compile()
    nodes = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

    assert "extract_one" in nodes
    assert "review_one" in nodes


def test_dedupe_and_edges_stay_whole_batch():
    """这两层需要全局视野（配对要看全部 draft、候选前置要看全部节点），扇不开。"""
    compiled = build_fanout_graph().compile()
    nodes = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

    assert "dedupe" in nodes and "dedupe_one" not in nodes
    assert "edges" in nodes and "edges_one" not in nodes


def test_end_to_end_produces_a_graph_that_passes_validation(tmp_path):
    """B 阶段独立于 A 阶段的端到端 happy path：全 fake 依赖，跑通六层
    （只是 extract/review 内部是扇出实现），产出应过 run_all 且 0 error。"""
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    findings = run_pipeline_fanout(
        source.parent, out, _fake_deps(), model_id="fake", curriculum="cn-moe-math-2022"
    )

    assert not has_errors(findings)
    assert (out / "graph.json").exists()
    assert (out / "02-drafts.json").exists()
    assert (out / "05-reviewed.json").exists()


def test_item_level_checkpoint_only_reruns_the_failed_chunk(tmp_path, monkeypatch):
    """B 阶段的核心卖点，也是与 A 阶段唯一有意义的对照点：条目级 checkpoint
    恢复时只重跑失败的那一条 chunk，不像 A 阶段 node_extract 整层重跑（含
    已成功的条目）。

    做法：monkeypatch extract_mod.extract_all，让"3.1.2"这个 chunk 第一次
    被调用时抛出、之后正常；同时把 RETRY_POLICY 调成 max_attempts=1，排除
    "Node 内部重试自愈"这条路径的干扰，逼真相只能来自 checkpoint resume。
    断言：resume 后"3.1.1"这个从未失败过的 chunk，其抽取总调用次数仍是 1
    —— 没有被搭车重跑。
    """
    source = tmp_path / "source"
    source.mkdir()
    source_path = source / "m.md"
    source_path.write_text(SOURCE, encoding="utf-8")
    db = tmp_path / "cp.sqlite"

    real_extract_all = extract_mod.extract_all
    calls: dict[str, int] = {}

    def flaky_extract_all(chunks, extractor):
        chunk = chunks[0]
        calls[chunk.standard_code] = calls.get(chunk.standard_code, 0) + 1
        if chunk.standard_code == "3.1.2" and calls[chunk.standard_code] == 1:
            raise RuntimeError("模拟第二个 chunk 的抽取持续失败（本次调用）")
        return real_extract_all(chunks, extractor)

    monkeypatch.setattr(graph_fanout_mod.extract_mod, "extract_all", flaky_extract_all)
    # 关掉 Node 级重试：只想验证 checkpoint resume 的粒度，不想让
    # RetryPolicy 在同一次调用内部悄悄自愈，掩盖掉要测的东西。
    monkeypatch.setattr(
        graph_fanout_mod, "RETRY_POLICY", RetryPolicy(max_attempts=1, retry_on=retry_on)
    )

    with pytest.raises(Exception):
        run_pipeline_fanout(
            source, tmp_path / "out", _fake_deps(), model_id="fake",
            curriculum="c", checkpoint_db=db, thread_id="t-item",
        )

    assert calls["3.1.1"] == 1
    assert calls["3.1.2"] == 1

    # 撤销故障，同一 thread_id 续跑
    monkeypatch.undo()
    monkeypatch.setattr(
        graph_fanout_mod, "RETRY_POLICY", RetryPolicy(max_attempts=1, retry_on=retry_on)
    )
    calls2: dict[str, int] = {}

    def counting_extract_all(chunks, extractor):
        chunk = chunks[0]
        calls2[chunk.standard_code] = calls2.get(chunk.standard_code, 0) + 1
        return real_extract_all(chunks, extractor)

    monkeypatch.setattr(graph_fanout_mod.extract_mod, "extract_all", counting_extract_all)

    findings = run_pipeline_fanout(
        source, tmp_path / "out", _fake_deps(), model_id="fake",
        curriculum="c", checkpoint_db=db, thread_id="t-item",
    )

    assert not has_errors(findings)
    # 核心断言：resume 后只有此前失败的 "3.1.2" 被重新调用一次；
    # 已经成功过的 "3.1.1" 完全没有被搭车重跑（不在 calls2 里，或次数为 0）。
    assert calls2.get("3.1.1", 0) == 0
    assert calls2["3.1.2"] == 1


def test_drops_from_a_failed_chunk_extraction_survive_on_disk(tmp_path):
    """extract_one 自己先落盘再返回 delta——同 A 阶段四个 LLM Node 的取舍，
    保证即便后续某条目/某层崩溃，已经算出的 drops 不会随进程一起消失。"""
    source = tmp_path / "source"
    source.mkdir()
    # 第二段没有编号 —— chunk 层应当丢弃并记账（与六层函数无关，验证的是
    # node_chunk 之外，extract_one 的丢弃记录也确实落了盘）
    (source / "m.md").write_text(SOURCE + "\n这一段没有编号。\n", encoding="utf-8")
    out = tmp_path / "out"

    deps = _fake_deps()

    def extractor_no_drafts(chunk):
        from cn_curriculum_graph.pipeline.models import DraftBatch

        return DraftBatch(drafts=[])

    deps.extractor = extractor_no_drafts

    run_pipeline_fanout(source, out, deps, model_id="fake", curriculum="c")

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    assert any(d["reason"] == "NO_DRAFTS" for d in drops)


def test_rerunning_a_completed_thread_does_not_accumulate_state(tmp_path):
    """全分支审查 Critical 1，fanout 版：`run_pipeline_fanout` 与
    `run_pipeline_lg` 共用同一段 resume 逻辑（`_ensure_consistent_resume` +
    `existing.next` 判断），且这里累加字段更多——`drafts`/`drops` 之外还有
    B 阶段专属的 `draft_review_kept`/`draft_review_outcomes`。跑完之后用
    同一 (checkpoint_db, thread_id) 再跑一次，产物必须与"只跑一次"一致，
    不能被上一轮已完成的 state 叠加污染。
    """
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"
    db = tmp_path / "cp.sqlite"

    run_pipeline_fanout(
        source.parent, out, _fake_deps(), model_id="fake",
        curriculum="c", checkpoint_db=db, thread_id="same-thread",
    )
    first_drafts = json.loads((out / "02-drafts.json").read_text(encoding="utf-8"))
    assert len(first_drafts) == 2

    findings = run_pipeline_fanout(
        source.parent, out, _fake_deps(), model_id="fake",
        curriculum="c", checkpoint_db=db, thread_id="same-thread",
    )

    second_drafts = json.loads((out / "02-drafts.json").read_text(encoding="utf-8"))
    assert len(second_drafts) == 2, (
        f"跑完之后再跑，02-drafts.json 不应比第一次多——实际：{len(second_drafts)} 条"
    )
    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert len(graph["topics"]) == 2, (
        f"跑完之后再跑，graph.json 的 topics 不应异常——实际：{len(graph['topics'])} 个"
    )
    assert not has_errors(findings)


def test_rerunning_a_completed_thread_with_different_args_raises(tmp_path):
    """fanout 版残余洞：thread 已跑完时换一套 source/out、复用同一
    thread_id，必须 raise，而不是静默接受、产出一份混杂两次实验数据的
    graph.json。"""
    source_a = tmp_path / "srcA"
    source_a.mkdir()
    (source_a / "m.md").write_text(SOURCE, encoding="utf-8")
    out_a = tmp_path / "outA"
    db = tmp_path / "cp.sqlite"

    run_pipeline_fanout(
        source_a, out_a, _fake_deps(), model_id="fake",
        curriculum="c", checkpoint_db=db, thread_id="shared-done",
    )

    source_b = tmp_path / "srcB"
    source_b.mkdir()
    (source_b / "m.md").write_text(SOURCE, encoding="utf-8")
    out_b = tmp_path / "outB"

    with pytest.raises(ValueError, match="source_dir"):
        run_pipeline_fanout(
            source_b, out_b, _fake_deps(), model_id="fake",
            curriculum="c", checkpoint_db=db, thread_id="shared-done",
        )

    assert not (out_b / "graph.json").exists()


def _make_source_with_n_items(tmp_path, n: int):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(
        "\n\n".join(f"3.1.{i} 条目{i}。" for i in range(1, n + 1)), encoding="utf-8"
    )
    return source.parent


def test_fanout_bounds_concurrent_extract_calls(tmp_path):
    """Important 1：`fan_out_chunks` 的并发度此前等于 chunk 数，没有任何
    上界——真实课标几十/几百个条目就是几十路并发 LLM 请求，直接撞 provider
    的速率限制。六层函数的逐条 try/except 会把每次 429 都吞成 DropRecord
    （不会崩，只会安静地把大半 draft 丢掉），`RetryPolicy` 也够不着（见
    graph.py 的 I1 论证）。

    用一个记录"同时在飞线程数"的 fake extractor + 6 个 chunk，断言
    `max_concurrency=2` 时峰值并发不超过 2。"""
    source = _make_source_with_n_items(tmp_path, 6)
    out = tmp_path / "out"

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def extractor(chunk):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1
        return DraftBatch(drafts=[_content(f"知识点{chunk.standard_code}", 1, chunk.text)])

    deps = _fake_deps()
    deps.extractor = extractor

    findings = run_pipeline_fanout(
        source, out, deps, model_id="fake", curriculum="c", max_concurrency=2,
    )

    assert not has_errors(findings)
    assert state["peak"] <= 2, f"并发峰值应被 max_concurrency=2 限制住，实际峰值：{state['peak']}"


def test_fanout_bounds_concurrent_review_calls(tmp_path):
    """同一个并发上限也要管住 `review_one`——它同样是 Send 扇出、同样对外
    发起真实 LLM 调用（fidelity_judges/name_judges），风险与 extract_one
    完全对称。"""
    source = _make_source_with_n_items(tmp_path, 6)
    out = tmp_path / "out"

    lock = threading.Lock()
    state = {"current": 0, "peak": 0}

    def fidelity_judge(draft):
        with lock:
            state["current"] += 1
            state["peak"] = max(state["peak"], state["current"])
        time.sleep(0.05)
        with lock:
            state["current"] -= 1
        return FidelityVerdict(reason="ok", judgment="faithful", reviewer="fake")

    deps = _fake_deps()
    deps.fidelity_judges = [fidelity_judge]

    findings = run_pipeline_fanout(
        source, out, deps, model_id="fake", curriculum="c", max_concurrency=2,
    )

    assert not has_errors(findings)
    assert state["peak"] <= 2, f"并发峰值应被 max_concurrency=2 限制住，实际峰值：{state['peak']}"


def test_default_concurrency_tracks_the_real_binding_constraint():
    """默认值必须跟着**真实约束**走，不能是一个魔数。

    实测校准（`scripts/calibrate_concurrency.py`，deepseek-v4-flash，
    2026-07-27，见 `docs/concurrency-calibration.md`）确定了三件事：

    1. provider 侧根本不是瓶颈 —— DeepSeek 官方文档给的是**账户级并发连接
       数**上限 2500(flash)/500(pro)，全程 0 个 429；原来的默认值 8 比它低
       两个数量级，"保守"这个词用错了地方。
    2. 真正的天花板在本机：每个 LLM 调用都走 `await asyncio.to_thread(...)`
       （为让 `NODE_TIMEOUT` 生效被逼出来的，见 graph.py 的 C1 记录），而
       `to_thread` 用的是 CPython 默认 executor，`max_workers =
       min(32, cpu_count + 4)`。并发设 32 时实测在飞峰值只有 20。
    3. 吞吐拐点正好压在这道天花板上：8→19 吞吐 6.49→12.60/s 一路涨，
       再往上到 24 反而回落到 12.09/s、p95 从 1458ms 涨到 2143ms（纯排队）。

    所以默认值 = 这道天花板本身。写成公式而不是抄下当天那台机器上的 19：
    换台机器 cpu 数不同，抄来的常数就又变回没有依据的魔数了。
    """
    assert graph_fanout_mod.DEFAULT_MAX_CONCURRENT_LLM_CALLS == min(32, (os.cpu_count() or 1) + 4)


def test_concurrency_above_the_thread_pool_ceiling_warns_instead_of_silently_capping():
    """设了拿不到的并发数必须出声。

    信号量放行 64 个任务，`asyncio.to_thread` 那边只有 ~19 个工人，多出来的
    只会排在线程池队列里 —— 现象是"我明明调到 64 了怎么没变快"，而代码里
    没有任何地方会告诉你原因。本项目对"静默"一贯的态度是：宁可吵，也不要
    让人对着一个不生效的旋钮调半天。
    """
    with pytest.warns(RuntimeWarning, match="线程池"):
        graph_fanout_mod.build_fanout_graph(max_concurrency=10_000)
