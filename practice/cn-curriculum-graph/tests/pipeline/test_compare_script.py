"""实验脚本的自测：度量口径必须可信，否则笔记里的数字是假的。"""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from compare_orchestration import (  # noqa: E402
    SCENARIOS,
    HardCrash,
    Scenario,
    ScenarioResult,
    run_scenario,
)


def test_scenarios_cover_the_four_designed_faults():
    """`rate_limit` 已按 Task 6 修复 4 改名为 `judge_exception_swallowed`
    （原名有限流语义误导性）；`partial_crash` 是修复 1 新增的场景。"""
    assert {s.name for s in SCENARIOS} == {
        "baseline",
        "judge_exception_swallowed",
        "hard_crash",
        "partial_crash",
    }


def test_baseline_completes_on_both_engines(tmp_path):
    baseline = next(s for s in SCENARIOS if s.name == "baseline")
    for engine in ("handwritten", "langgraph"):
        result = run_scenario(engine, baseline, tmp_path / engine)
        assert isinstance(result, ScenarioResult)
        assert result.round1_crashed is False
        assert result.crash_exception is None
        assert result.final_completed is True
        assert result.total_calls > 0
        # baseline 无故障，不该触发"第二轮恢复重跑"
        assert result.recovery_calls == 0


def test_judge_exception_is_swallowed_as_drop_record_on_both_engines(tmp_path):
    """已实测结论（见 graph.py 的 I1 注释）：挂在 deps 上的故障会被六层函数的
    逐条 try/except 吞成 DropRecord，从不让 Node/整层抛出异常 —— 也就从不
    触发 Node 级 RetryPolicy、更不会让流水线整体失败。两个引擎在这个场景下
    应该都『没有崩溃、不需要恢复重跑』，行为一致。

    场景名故意不叫 `rate_limit`（Task 6 修复 4）：这里的注入没有任何限流
    语义（`times=1` 只失败一次，没有 `Retry-After`、没有退避、没有连续
    失败），真正在测的是"判定器异常被六层函数的逐条 try/except 吞成
    DropRecord"这条故障吞没边界，与两个引擎的编排机制无关。
    """
    scenario = next(s for s in SCENARIOS if s.name == "judge_exception_swallowed")
    for engine in ("handwritten", "langgraph"):
        result = run_scenario(engine, scenario, tmp_path / engine)
        assert result.round1_crashed is False
        assert result.crash_exception is None
        assert result.final_completed is True
        assert result.recovery_calls == 0


def test_hard_crash_shows_rerun_cost_difference(tmp_path):
    """手写版没有重入能力，崩溃后第二次要从头跑；LangGraph 版从断点续。
    这个差值就是笔记第一章的核心数字。"""
    hard_crash = next(s for s in SCENARIOS if s.name == "hard_crash")
    hw = run_scenario("handwritten", hard_crash, tmp_path / "hw")
    lg = run_scenario("langgraph", hard_crash, tmp_path / "lg")

    assert hw.round1_crashed is True
    assert lg.round1_crashed is True
    assert hw.crash_exception == "HardCrash"
    assert lg.crash_exception == "HardCrash"
    assert hw.final_completed is True
    assert lg.final_completed is True
    assert hw.recovery_calls > lg.recovery_calls


def test_partial_crash_burns_real_calls_unlike_hard_crash(tmp_path):
    """Task 6 修复 1：`hard_crash` 整层立即抛，重试 3 次全是零调用——重试
    的代价被白送。`partial_crash` 处理完前一半有候选池的 target 之后才抛，
    每次重试都真实烧调用。用第一轮的调用数（total_calls - recovery_calls）
    对照验证：partial_crash 的第一轮调用数必须严格大于 hard_crash 的。
    """
    partial = next(s for s in SCENARIOS if s.name == "partial_crash")
    hard = next(s for s in SCENARIOS if s.name == "hard_crash")
    for engine in ("handwritten", "langgraph"):
        p = run_scenario(engine, partial, tmp_path / f"p-{engine}")
        h = run_scenario(engine, hard, tmp_path / f"h-{engine}")

        assert p.round1_crashed is True
        assert p.crash_exception == "HardCrash"

        p_first_round_calls = p.total_calls - p.recovery_calls
        h_first_round_calls = h.total_calls - h.recovery_calls
        assert p_first_round_calls > h_first_round_calls


def test_partial_crash_still_favors_langgraph_but_by_less_than_hard_crash(tmp_path):
    """审查者的核心论点：`hard_crash` 把重试代价降为零，虚抬了 LangGraph 的
    净优势；`partial_crash` 让重试代价可见后，净优势应该更小（但不消失，
    LangGraph 仍能跳过 extract/dedupe 两层）。"""
    partial = next(s for s in SCENARIOS if s.name == "partial_crash")
    hard = next(s for s in SCENARIOS if s.name == "hard_crash")

    p_hw = run_scenario("handwritten", partial, tmp_path / "p-hw")
    p_lg = run_scenario("langgraph", partial, tmp_path / "p-lg")
    h_hw = run_scenario("handwritten", hard, tmp_path / "h-hw")
    h_lg = run_scenario("langgraph", hard, tmp_path / "h-lg")

    partial_gap = p_hw.total_calls - p_lg.total_calls
    hard_gap = h_hw.total_calls - h_lg.total_calls
    assert 0 <= partial_gap < hard_gap


def test_chunks_parameter_scales_the_fixture(tmp_path):
    """Task 6 修复 2：`--chunks N` 应该真的改变合成素材规模，而不是被忽略。"""
    baseline = next(s for s in SCENARIOS if s.name == "baseline")
    small = run_scenario("handwritten", baseline, tmp_path / "small", chunks=3)
    big = run_scenario("handwritten", baseline, tmp_path / "big", chunks=10)
    assert big.total_calls > small.total_calls


def test_judges_per_category_parameter_changes_call_volume(tmp_path):
    """Task 6 修复 3：judges_per_category 参数必须真的生效——每类多挂一个
    judge，总调用数应该增加。"""
    baseline = next(s for s in SCENARIOS if s.name == "baseline")
    one = run_scenario(
        "handwritten", baseline, tmp_path / "j1", judges_per_category=1
    )
    two = run_scenario(
        "handwritten", baseline, tmp_path / "j2", judges_per_category=2
    )
    assert two.total_calls > one.total_calls


def test_unexpected_exception_type_raises_instead_of_silently_swallowing(tmp_path):
    """Task 6 修复 5.1：脚本必须能分辨"按注入原因崩了"和"因为别的 bug 崩了"。
    如果第一轮抛出的异常类型和场景预期的不一致，脚本要响亮地报错，而不是
    把它悄悄计成本场景的"恢复重跑"数据。"""

    def _boom_wrong_type(drafts, proposer):
        raise ValueError("这不是场景预期的 HardCrash")

    bogus = Scenario(
        name="bogus-wrong-type",
        dep_specs=(),
        crash_fn=_boom_wrong_type,
        expected_exception=HardCrash,
        in_main_table=True,
        note="测试用：故意抛一个场景没预期的异常类型",
    )
    with pytest.raises(AssertionError):
        run_scenario("handwritten", bogus, tmp_path / "bogus-wrong-type")


def test_scenario_without_expected_crash_raises_if_it_crashes_anyway(tmp_path):
    """Task 6 修复 5.1：`expected_exception=None` 意味着这个场景设计上不该崩溃
    （如 baseline / judge_exception_swallowed）。如果它意外崩了，那是别的 bug，
    脚本要报错，不能悄悄当成"这个场景需要恢复重跑"处理。"""

    def _boom_unexpected(drafts, proposer):
        raise HardCrash("这个场景不该崩，这里故意让它崩来测试断言")

    bogus = Scenario(
        name="bogus-unexpected-crash",
        dep_specs=(),
        crash_fn=_boom_unexpected,
        expected_exception=None,
        in_main_table=True,
        note="测试用：不该崩却崩了",
    )
    with pytest.raises(AssertionError):
        run_scenario("handwritten", bogus, tmp_path / "bogus-unexpected-crash")


def test_judge_exception_scenario_is_marked_out_of_the_main_table():
    """Task 6 修复 4：这个场景不该混进"完成/总调用/恢复重跑"对比表——两个
    引擎在这里数字完全相同，混排会被误读成"两个引擎在故障下表现一致"，
    而实际是"这个注入方式压根没触发任何重试机制"。"""
    scenario = next(s for s in SCENARIOS if s.name == "judge_exception_swallowed")
    assert scenario.in_main_table is False
    for name in ("baseline", "hard_crash", "partial_crash"):
        assert next(s for s in SCENARIOS if s.name == name).in_main_table is True
