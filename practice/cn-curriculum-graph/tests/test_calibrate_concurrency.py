"""并发爬坡校准脚本的契约测试 —— 全程注入 fake caller，不触网、不需要 key。

脚本本身要打真实 API 才有意义，但"打出去"和"怎么统计、怎么下结论"是两件事。
这里测的是后者：信号量真的封住了并发上界吗？429 有没有和其他失败混为一谈？
一个整档全挂的场景会不会把整轮爬坡带崩？推荐值是按"最小够用"选的还是按
"最大吞吐"选的？

刻意用**阻塞的 sleep + `asyncio.to_thread`** 而不是 `await asyncio.sleep`：
生产路径（`graph_fanout` 的两处扇出）底下是阻塞的 anthropic SDK HTTP 调用，
靠 `asyncio.to_thread` 扔进线程池才有并发。用 `await asyncio.sleep` 测不出
**默认线程池那道隐藏上界**（`min(32, cpu+4)`）——而那恰恰是本次校准最重要的
发现之一。
"""

from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from calibrate_concurrency import LevelResult, recommend, run_level  # noqa: E402


def _sleeping_caller(seconds: float = 0.05):
    def call(i: int) -> None:
        time.sleep(seconds)

    return call


class _FakeRateLimit(Exception):
    status_code = 429


def test_semaphore_actually_bounds_peak_in_flight():
    """上界不是写在文档里的承诺，是能量出来的数字。"""
    in_flight = {"now": 0, "peak": 0}
    lock = __import__("threading").Lock()

    def call(i: int) -> None:
        with lock:
            in_flight["now"] += 1
            in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
        time.sleep(0.05)
        with lock:
            in_flight["now"] -= 1

    result = asyncio.run(run_level(call, requests=12, concurrency=4))

    assert in_flight["peak"] <= 4
    assert result.peak_in_flight <= 4
    assert result.ok == 12


def test_rate_limited_calls_are_counted_apart_from_other_failures():
    """429 和"别的炸了"必须分开记 —— 校准的整个目的就是找 429 的拐点，
    把它和网络抖动、schema 错误混在一个 `failed` 里，这份数据就废了。"""

    def call(i: int) -> None:
        if i % 3 == 0:
            raise _FakeRateLimit("429 Too Many Requests")
        if i % 3 == 1:
            raise RuntimeError("别的错")

    result = asyncio.run(run_level(call, requests=9, concurrency=3))

    assert result.rate_limited == 3
    assert result.failed == 3
    assert result.ok == 3


def test_a_rate_limit_is_recognised_from_the_message_when_there_is_no_status_code():
    """不同 SDK 表达 429 的方式不一样（有的挂 `status_code`，有的只在消息里）。
    校准脚本不该假定用的是哪个 SDK 的哪个版本。"""

    def call(i: int) -> None:
        raise RuntimeError("Error code: 429 - rate limit exceeded")

    result = asyncio.run(run_level(call, requests=3, concurrency=2))

    assert result.rate_limited == 3
    assert result.failed == 0


def test_latency_stats_come_from_successful_calls_only():
    """失败调用往往秒回（连接直接被拒），混进延迟统计会把 p95 压得很好看，
    读表的人会得出"这档很健康"的相反结论。"""

    def call(i: int) -> None:
        if i == 0:
            raise _FakeRateLimit("429")  # 瞬间失败
        time.sleep(0.05)

    result = asyncio.run(run_level(call, requests=4, concurrency=1))

    assert result.ok == 3
    assert result.p50_ms >= 40, f"p50={result.p50_ms}ms，失败调用的 0ms 混进来了"


def test_a_level_that_fails_completely_still_returns_a_result():
    """整档全挂是**数据点**，不是异常。爬坡跑到高档位撞墙是预期行为，
    脚本必须把它记下来继续跑完，而不是抛出去让整轮校准前功尽弃。"""

    def call(i: int) -> None:
        raise _FakeRateLimit("429")

    result = asyncio.run(run_level(call, requests=5, concurrency=5))

    assert result.ok == 0
    assert result.rate_limited == 5
    assert result.p50_ms == 0.0  # 没有成功样本时不编造分位数


def _r(concurrency: int, throughput: float, rate_limited: int = 0) -> LevelResult:
    ok = 10
    return LevelResult(
        concurrency=concurrency, requests=ok + rate_limited, ok=ok,
        rate_limited=rate_limited, failed=0, wall_s=ok / throughput,
        p50_ms=1.0, p95_ms=1.0, peak_in_flight=concurrency,
    )


def test_recommendation_picks_the_smallest_level_that_is_already_fast_enough():
    """选"最小够用"而不是"最大吞吐"。

    并发不是免费的：多一路在飞的请求就多一份账户级配额占用、多一份下游
    限流风险、多一份崩溃时白烧的调用量。8→16 只换来 3% 吞吐时，那 3% 不值
    多一倍的并发——所以规则是"取吞吐达到峰值 95% 的**最小**档位"。
    """
    results = [_r(1, 1.0), _r(2, 1.9), _r(4, 3.5), _r(8, 6.0), _r(16, 6.1), _r(32, 6.0)]

    assert recommend(results) == 8


def test_recommendation_never_picks_a_level_that_saw_rate_limiting():
    """撞到 429 的档位一律出局，哪怕它的吞吐最高 —— 429 意味着已经越界，
    此时的吞吐数字是"侥幸挤进去的那部分"，不可复现。"""
    results = [_r(4, 3.5), _r(8, 6.0), _r(16, 9.0, rate_limited=4)]

    assert recommend(results) == 8


def test_recommendation_falls_back_to_one_when_every_level_was_rate_limited():
    """全线撞墙时不能返回 None 让调用方去猜，也不能返回撞墙的档位。"""
    results = [_r(4, 3.5, rate_limited=1), _r(8, 6.0, rate_limited=9)]

    assert recommend(results) == 1


def test_recommendation_needs_at_least_one_result():
    with pytest.raises(ValueError):
        recommend([])
