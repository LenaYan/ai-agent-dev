"""实验脚本的自测：度量口径必须可信，否则笔记里的数字是假的。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from compare_orchestration import SCENARIOS, ScenarioResult, run_scenario  # noqa: E402


def test_scenarios_cover_the_three_designed_faults():
    assert {s.name for s in SCENARIOS} == {"baseline", "rate_limit", "hard_crash"}


def test_baseline_completes_on_both_engines(tmp_path):
    baseline = next(s for s in SCENARIOS if s.name == "baseline")
    for engine in ("handwritten", "langgraph"):
        result = run_scenario(engine, baseline, tmp_path / engine)
        assert isinstance(result, ScenarioResult)
        assert result.completed is True
        assert result.total_calls > 0
        # baseline 无故障，不该触发"第二轮恢复重跑"
        assert result.recovery_calls == 0


def test_rate_limit_is_swallowed_as_drop_record_on_both_engines(tmp_path):
    """已实测结论（见 graph.py 的 I1 注释）：挂在 deps 上的故障会被六层函数的
    逐条 try/except 吞成 DropRecord，从不让 Node/整层抛出异常 —— 也就从不
    触发 Node 级 RetryPolicy、更不会让流水线整体失败。两个引擎在这个场景下
    应该都『没有崩溃、不需要恢复重跑』，行为一致。
    """
    rate_limit = next(s for s in SCENARIOS if s.name == "rate_limit")
    for engine in ("handwritten", "langgraph"):
        result = run_scenario(engine, rate_limit, tmp_path / engine)
        assert result.completed is True
        assert result.recovery_calls == 0


def test_hard_crash_shows_rerun_cost_difference(tmp_path):
    """手写版没有重入能力，崩溃后第二次要从头跑；LangGraph 版从断点续。
    这个差值就是笔记第一章的核心数字。"""
    hard_crash = next(s for s in SCENARIOS if s.name == "hard_crash")
    hw = run_scenario("handwritten", hard_crash, tmp_path / "hw")
    lg = run_scenario("langgraph", hard_crash, tmp_path / "lg")

    assert hw.completed is True
    assert lg.completed is True
    assert hw.recovery_calls > lg.recovery_calls
