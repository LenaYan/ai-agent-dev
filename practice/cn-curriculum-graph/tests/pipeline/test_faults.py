"""故障注入装置的自测。装置不可信，实验数据就不可信。

零生产代码改动 —— 因为 PipelineDeps 本来就是依赖注入的。
当初为可测性做的 DI，现在直接变成了实验装置。
"""

import pytest

from cn_curriculum_graph.pipeline.faults import CallCounter, FaultSpec, wrap_deps
from cn_curriculum_graph.pipeline.models import DraftBatch


class _Boom(Exception):
    pass


def _deps_with_counting_extractor():
    """构造一个最小 PipelineDeps，extractor 每次调用返回空 batch。"""
    from cn_curriculum_graph.pipeline.run import PipelineDeps

    return PipelineDeps(
        extractor=lambda chunk: DraftBatch(drafts=[]),
        same_topic_judge=lambda a, b: None,
        edge_proposer=lambda t, c: None,
        fidelity_judges=[lambda d: None],
        name_judges=[lambda name, description: None],
        edge_judges=[lambda t, e: None],
    )


def test_counts_every_call_per_target():
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[])

    deps.extractor("chunk1")
    deps.extractor("chunk2")

    assert counter.counts["extractor"] == 2


def test_raises_on_the_specified_call_only():
    spec = FaultSpec(target="extractor", fail_on_call=2, exc=_Boom, times=1)
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[spec])

    deps.extractor("c1")                       # 第 1 次：正常
    with pytest.raises(_Boom):
        deps.extractor("c2")                   # 第 2 次：炸
    deps.extractor("c3")                       # 第 3 次：恢复正常

    assert counter.counts["extractor"] == 3


def test_times_controls_how_many_consecutive_calls_fail():
    spec = FaultSpec(target="extractor", fail_on_call=1, exc=_Boom, times=2)
    deps, _ = wrap_deps(_deps_with_counting_extractor(), specs=[spec])

    with pytest.raises(_Boom):
        deps.extractor("c1")
    with pytest.raises(_Boom):
        deps.extractor("c2")
    deps.extractor("c3")   # 第 3 次不再炸


def test_wraps_judges_inside_lists():
    """judges 是列表，包裹器要能钻进列表里逐个包。"""
    spec = FaultSpec(target="fidelity_judges", fail_on_call=1, exc=_Boom, times=1)
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[spec])

    with pytest.raises(_Boom):
        deps.fidelity_judges[0]("draft")

    assert counter.counts["fidelity_judges"] == 1


def test_counter_is_reset_between_runs():
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[])
    deps.extractor("c1")

    counter.reset()

    assert counter.counts == {}


def test_unknown_target_is_rejected_loudly():
    """写错 target 名字就该当场炸 —— 否则实验会静默地什么都没注入。"""
    with pytest.raises(ValueError, match="没有这个依赖项"):
        wrap_deps(_deps_with_counting_extractor(), specs=[FaultSpec(target="typo", fail_on_call=1, exc=_Boom)])


def test_wrap_deps_does_not_mutate_original_deps():
    """自己补的测试（brief 未覆盖）：wrap_deps 必须返回副本，不能就地改原 deps。

    动机：后续对比实验要反复用同一份 deps 构造多个故障变体，
    如果 wrap_deps 就地修改了原 deps，多个变体之间会互相污染
    （比如变体 A 包裹后原 deps.extractor 已经变成代理，变体 B 再包一层
    就是代理套代理，计数与故障触发都会失真）。
    """
    original = _deps_with_counting_extractor()
    original_extractor = original.extractor
    original_same_topic_judge = original.same_topic_judge
    original_fidelity_judges = original.fidelity_judges

    spec = FaultSpec(target="extractor", fail_on_call=1, exc=_Boom, times=1)
    wrapped, _ = wrap_deps(original, specs=[spec])

    # 返回的是不同对象
    assert wrapped is not original
    # 原 deps 的可调用字段引用未被替换
    assert original.extractor is original_extractor
    assert original.same_topic_judge is original_same_topic_judge
    assert original.fidelity_judges is original_fidelity_judges
    # 原 extractor 调用时不受故障注入影响，也不计数
    assert original.extractor("chunk") == DraftBatch(drafts=[])
