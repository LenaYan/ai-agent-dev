"""B 阶段：条目级扇出。

⚠️ 这已不是"同一需求的两种实现" —— 架构变了。它回答的是另一个问题
（框架解锁了什么），笔记里必须与第一章分开读。

**不测什么**：不接入 test_run.py 那 13 条两引擎对等的参数化端到端测试——
B 阶段的架构已经偏离"与手写版对等"这条硬标准，硬凑对等测试等于假装
两者仍在回答同一个问题。这里只测 B 阶段自己的结构与行为。
"""

from __future__ import annotations

import json

import pytest
from langgraph.types import RetryPolicy

from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import graph_fanout as graph_fanout_mod
from cn_curriculum_graph.pipeline.graph import retry_on
from cn_curriculum_graph.pipeline.graph_fanout import build_fanout_graph, run_pipeline_fanout
from cn_curriculum_graph.runner import has_errors

from .test_run import SOURCE, _fake_deps


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
