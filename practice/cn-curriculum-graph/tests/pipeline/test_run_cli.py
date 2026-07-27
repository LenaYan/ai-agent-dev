"""`ccg-generate` 命令行的接线测试。

**为什么现在补**：并发校准量出了一个默认值，但在此之前 `--engine` 只有
`handwritten`/`langgraph` 两个选项——**扇出版根本没有生产入口**，
`max_concurrency` 这个旋钮只有 `scripts/compare_orchestration.py` 和测试碰得到。
一个校准好的参数如果没有任何生产路径能用上它，校准就只是自娱自乐。

这里只测"接线"：参数有没有原样传到对的函数、非法组合会不会当场退出。
流水线本身的行为由 `test_run.py` / `test_graph_fanout.py` 覆盖，不重复。
"""

from __future__ import annotations

import pytest

from cn_curriculum_graph.pipeline import graph_fanout as graph_fanout_mod
from cn_curriculum_graph.pipeline import run as run_mod


@pytest.fixture(autouse=True)
def _fake_key_and_deps(monkeypatch):
    """CLI 会检查 key、会构造真 DeepSeek 客户端 —— 两样都挡掉，不触网。"""
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test")
    monkeypatch.setattr(run_mod, "build_deepseek_deps", lambda models: "DEPS")


def _capture(monkeypatch, module, name: str) -> dict:
    """顶替的是**源模块**上的函数，不是 run.py 里的名字。

    `run.main()` 对 graph/graph_fanout 是延迟 import（模块顶层 import 会与
    它们对 `PipelineDeps` 的 import 形成循环），调用时才去读模块属性 ——
    所以顶替源模块才拦得住。
    """
    seen: dict = {}

    def fake(*args, **kwargs):
        seen["args"] = args
        seen["kwargs"] = kwargs
        return []

    monkeypatch.setattr(module, name, fake)
    return seen


def test_fanout_engine_is_reachable_from_the_cli(tmp_path, monkeypatch):
    """B 阶段扇出版必须有生产入口。没有它，条目级 checkpoint 与 8× 墙钟
    加速这两项"框架额外解锁的能力"就永远只活在实验脚本里。"""
    seen = _capture(monkeypatch, graph_fanout_mod, "run_pipeline_fanout")

    code = run_mod.main(
        ["--engine", "langgraph-fanout", "--source", str(tmp_path), "--out", str(tmp_path / "o")]
    )

    assert code == 0
    assert seen["kwargs"]["max_concurrency"] == graph_fanout_mod.DEFAULT_MAX_CONCURRENT_LLM_CALLS


def test_max_concurrency_flag_is_passed_through_verbatim(tmp_path, monkeypatch):
    seen = _capture(monkeypatch, graph_fanout_mod, "run_pipeline_fanout")

    run_mod.main(
        [
            "--engine", "langgraph-fanout", "--max-concurrency", "4",
            "--source", str(tmp_path), "--out", str(tmp_path / "o"),
        ]
    )

    assert seen["kwargs"]["max_concurrency"] == 4


@pytest.mark.parametrize("engine", ["handwritten", "langgraph"])
def test_max_concurrency_is_rejected_for_engines_that_have_no_fanout(
    engine, tmp_path, monkeypatch, capsys
):
    """手写版和 A 阶段都没有扇出点，`--max-concurrency` 对它们无处可传。

    静默忽略是最坏的处理：使用者会以为自己限住了并发，实际上 A 阶段的
    Node 粒度执行本来就是串行的、手写版更是逐条同步。与其让人对着一个
    根本不接线的参数建立错误预期，不如当场退出——和已有的
    `--checkpoint 仅 langgraph 支持` 是同一条原则。
    """
    with pytest.raises(SystemExit) as exit_info:
        run_mod.main(
            [
                "--engine", engine, "--max-concurrency", "4",
                "--source", str(tmp_path), "--out", str(tmp_path / "o"),
            ]
        )

    assert exit_info.value.code == 2
    assert "max-concurrency" in capsys.readouterr().err


def test_fanout_engine_supports_checkpoint_like_the_a_stage(tmp_path, monkeypatch):
    """扇出版的 checkpoint 粒度是**条目级**（A 阶段是层级），这正是 B 阶段
    存在的理由之一，CLI 不接上等于白做。"""
    seen = _capture(monkeypatch, graph_fanout_mod, "run_pipeline_fanout")

    run_mod.main(
        [
            "--engine", "langgraph-fanout", "--checkpoint", str(tmp_path / "cp.sqlite"),
            "--source", str(tmp_path), "--out", str(tmp_path / "o"),
        ]
    )

    assert seen["kwargs"]["checkpoint_db"] == tmp_path / "cp.sqlite"
    assert seen["kwargs"]["thread_id"]  # 由 --source/--out 派生，不是写死的 "default"
