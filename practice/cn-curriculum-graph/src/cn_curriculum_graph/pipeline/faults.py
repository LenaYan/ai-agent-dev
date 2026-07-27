"""受控实验的故障注入装置。

把 PipelineDeps 里每个可调用项换成「计数 + 可控失败」的代理，
**不改任何生产代码** —— 因为 deps 本来就是依赖注入的。

这本身是对比笔记的一条素材：当初为可测性做的 DI，
现在原封不动变成了实验装置。
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FaultSpec:
    """在第 fail_on_call 次调用 target 时开始抛 exc，连抛 times 次。

    列表型字段（fidelity_judges / name_judges / edge_judges）的计数语义：
    计数器按 target 名字**共享**，不是列表里每个元素各计各的。也就是说
    fail_on_call=2 命中的是「列表里所有元素合计调用次数达到第 2 次」的
    那次调用——如果列表里有两个 judge，且各自被调用一次，命中的是第二个
    judge 的第一次调用，而不是某个特定 judge 自己的第二次调用。当前装置
    做不到「精确指定列表里第几个判定器失败」这种粒度。
    """

    target: str
    fail_on_call: int
    exc: type[Exception]
    times: int = 1

    def __post_init__(self) -> None:
        if self.fail_on_call < 1:
            raise ValueError(
                f"fail_on_call 必须 >= 1，收到 {self.fail_on_call!r}"
                "（否则故障永远不会触发，实验会静默地什么都没注入）"
            )
        if self.times < 1:
            raise ValueError(
                f"times 必须 >= 1，收到 {self.times!r}"
                "（否则故障永远不会触发，实验会静默地什么都没注入）"
            )


@dataclass
class CallCounter:
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def reset(self) -> None:
        self.counts = defaultdict(int)


def _proxy(fn: Any, name: str, counter: CallCounter, specs: list[FaultSpec]) -> Any:
    mine = [s for s in specs if s.target == name]

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        counter.counts[name] += 1
        n = counter.counts[name]
        for spec in mine:
            if spec.fail_on_call <= n < spec.fail_on_call + spec.times:
                raise spec.exc(f"注入故障：{name} 第 {n} 次调用")
        return fn(*args, **kwargs)

    return wrapped


def wrap_deps(deps: Any, specs: list[FaultSpec]) -> tuple[Any, CallCounter]:
    """返回一份包裹过的 deps 副本与计数器。原 deps 不被修改。"""
    counter = CallCounter()
    field_names = {f.name for f in dataclasses.fields(deps)}

    for spec in specs:
        if spec.target not in field_names:
            raise ValueError(f"没有这个依赖项：{spec.target}（可用：{sorted(field_names)}）")

    changes: dict[str, Any] = {}
    for name in field_names:
        value = getattr(deps, name)
        if isinstance(value, list):
            changes[name] = [_proxy(v, name, counter, specs) for v in value]
        else:
            changes[name] = _proxy(value, name, counter, specs)

    return dataclasses.replace(deps, **changes), counter
