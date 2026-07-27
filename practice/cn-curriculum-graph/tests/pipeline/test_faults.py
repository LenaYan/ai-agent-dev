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


def _deps_with_two_fidelity_judges():
    """构造一个 fidelity_judges 含两个元素的 PipelineDeps，用于验证「共享计数」语义。"""
    from cn_curriculum_graph.pipeline.run import PipelineDeps

    return PipelineDeps(
        extractor=lambda chunk: DraftBatch(drafts=[]),
        same_topic_judge=lambda a, b: None,
        edge_proposer=lambda t, c: None,
        fidelity_judges=[lambda d: "judge_a", lambda d: "judge_b"],
        name_judges=[lambda name, description: None],
        edge_judges=[lambda t, e: None],
    )


def test_list_target_counter_is_shared_across_elements_not_per_element():
    """固化装置的真实语义：列表型 target 的计数按「字段名」共享，不是按列表元素各自独立计数。

    两个 judge（A、B）在同一个 fidelity_judges 列表里。fail_on_call=2 命中的是
    「A 调一次、B 调一次」之后的那次调用——也就是 B 的第一次调用，而不是
    某个特定 judge 自己的第二次调用。如果调用方以为「每个 judge 各计各的」，
    这里就会直接证伪。
    """
    spec = FaultSpec(target="fidelity_judges", fail_on_call=2, exc=_Boom, times=1)
    deps, counter = wrap_deps(_deps_with_two_fidelity_judges(), specs=[spec])

    judge_a, judge_b = deps.fidelity_judges

    # 第 1 次调用（judge A）：正常，尚未命中 fail_on_call=2
    assert judge_a("draft") == "judge_a"

    # 第 2 次调用是 judge B 的第一次调用（不是 judge A 的第二次）——共享计数下应在此处炸
    with pytest.raises(_Boom):
        judge_b("draft")

    assert counter.counts["fidelity_judges"] == 2


def test_counter_is_reset_between_runs():
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[])
    deps.extractor("c1")

    counter.reset()

    assert counter.counts == {}


def test_unknown_target_is_rejected_loudly():
    """写错 target 名字就该当场炸 —— 否则实验会静默地什么都没注入。"""
    with pytest.raises(ValueError, match="没有这个依赖项"):
        wrap_deps(_deps_with_counting_extractor(), specs=[FaultSpec(target="typo", fail_on_call=1, exc=_Boom)])


@pytest.mark.parametrize("bad_fail_on_call", [0, -1])
def test_non_positive_fail_on_call_is_rejected_loudly(bad_fail_on_call):
    """fail_on_call < 1 之前会静默地永不触发——那样的实验会正常跑完并产出一张毫无意义的数据表。

    必须在构造 FaultSpec 时就当场抛 ValueError，而不是让故障装置悄悄失效。
    """
    with pytest.raises(ValueError, match="fail_on_call"):
        FaultSpec(target="extractor", fail_on_call=bad_fail_on_call, exc=_Boom)


@pytest.mark.parametrize("bad_times", [0, -1])
def test_non_positive_times_is_rejected_loudly(bad_times):
    """times < 1 同理——静默地一次都不触发，实验数据看着正常、实际毫无意义。"""
    with pytest.raises(ValueError, match="times"):
        FaultSpec(target="extractor", fail_on_call=1, exc=_Boom, times=bad_times)


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

    # 对 fidelity_judges 注入一个必然触发的故障（fail_on_call=1），用于下面的行为检查：
    # 如果 wrap_deps 把列表元素就地替换成了代理（而不是新建列表），
    # 原列表里的元素也会变成会抛异常的代理——即便 `original.fidelity_judges is
    # original_fidelity_judges` 这个引用检查仍然通过（列表对象本身没变，
    # 只是内容被换了）。所以只查 `is` 同一性查不出这种就地替换。
    spec_extractor = FaultSpec(target="extractor", fail_on_call=1, exc=_Boom, times=1)
    spec_judges = FaultSpec(target="fidelity_judges", fail_on_call=1, exc=_Boom, times=1)
    wrapped, _ = wrap_deps(original, specs=[spec_extractor, spec_judges])

    # 返回的是不同对象
    assert wrapped is not original
    # 原 deps 的可调用字段引用未被替换
    assert original.extractor is original_extractor
    assert original.same_topic_judge is original_same_topic_judge
    assert original.fidelity_judges is original_fidelity_judges
    # 原 extractor 调用时不受故障注入影响，也不计数
    assert original.extractor("chunk") == DraftBatch(drafts=[])
    # 行为检查（补强 is 同一性检查的盲区）：原列表里的元素必须仍是裸函数，
    # 调用它不该抛出 spec_judges 注入的异常——否则说明列表内容被就地替换过。
    original.fidelity_judges[0]("draft")
