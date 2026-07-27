"""并发爬坡校准：把 `DEFAULT_MAX_CONCURRENT_LLM_CALLS` 从工程判断换成实测数字。

## 为什么需要这个脚本

`graph_fanout.DEFAULT_MAX_CONCURRENT_LLM_CALLS` 原来是 8，注释里如实写着
"工程判断，非厂商 SLA 承诺——未做任何 provider 端实测校准"。上量前要么把它
量出来，要么承认这个数字没有依据。

**先查文档，发现问题比想象的复杂**（DeepSeek 官方 API 文档
<https://api-docs.deepseek.com/quick_start/rate_limit>，2026-07-27 核实）：

- DeepSeek **不设 RPM/TPM 配额**，只设**账户级并发连接数**上限：
  `deepseek-v4-flash` 2500、`deepseek-v4-pro` 500，超过返 HTTP 429。
- 一个请求从发出到响应完成期间占用一个并发连接；上限按**账户**算，与用了
  几个 API key 无关。
- 服务端繁忙时不拒绝，而是**拖慢响应**（非流式回空行、流式回
  `: keep-alive` 注释保活），10 分钟未开始推理才断开。

也就是说：**默认值 8 相对 provider 配额低了两个数量级**（flash 差 300 倍）。
所以"保守"这个词用错了地方——真正的瓶颈根本不在 provider 那边。那在哪？

## 三道上界，只有一道是 provider 给的

| 上界 | 数值 | 谁定的 |
|---|---|---|
| 账户级并发连接 | 2500 (flash) / 500 (pro) | DeepSeek |
| **`asyncio.to_thread` 的默认线程池** | **`min(32, cpu_count + 4)`** | CPython |
| 我们自己的信号量 | `DEFAULT_MAX_CONCURRENT_LLM_CALLS` | 本项目 |

中间那道最容易被忽略，也是本次校准最重要的发现：生产路径上每个 LLM 调用都
走 `await asyncio.to_thread(...)`（那是为了让 `NODE_TIMEOUT` 真正生效而被逼
出来的，见 `graph.py` 的 C1 修复记录），而 `to_thread` 用的是事件循环的
**默认 executor**，`ThreadPoolExecutor` 默认 `max_workers = min(32,
os.cpu_count() + 4)`。把信号量调到这个数以上**不会**带来更多真并发——多出来
的任务只会排在线程池队列里。所以本脚本一定要用"阻塞函数 + `to_thread`"来测，
用 `await asyncio.sleep` 那种纯协程写法会测出一条看不到这道墙的假曲线。

## 怎么读这份数据

关注三列：`rate_limited`（撞没撞到 429）、`throughput`（有效吞吐，只算成功
调用）、`peak`（实际观测到的在飞并发——它若明显低于设定的并发数，说明撞到
的是线程池而不是 provider）。推荐值由 `recommend()` 给出，规则是**取吞吐达到
峰值 95% 的最小档位**，理由见该函数文档。

## 怎么跑

    DEEPSEEK_API_KEY=... uv run python scripts/calibrate_concurrency.py \
        --levels 1,2,4,8,16,32 --requests 20 --model deepseek-v4-flash

成本：每次请求是一次最小的 name/description 一致性判定（约 100 token 输入、
十几 token 输出）。120 次调用量级在 flash 上远小于 $0.01。
"""

from __future__ import annotations

import argparse
import asyncio
import os
import statistics
import sys
import threading
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

# 每档发多少次请求。太少则分位数没有意义，太多则纯烧钱——20 是在
# "p95 至少由 1 个真实样本决定"和"总调用量可控"之间取的。
DEFAULT_REQUESTS_PER_LEVEL = 20
DEFAULT_LEVELS = (1, 2, 4, 8, 16, 32)


@dataclass(frozen=True)
class LevelResult:
    """一个并发档位的观测结果。

    `ok`/`rate_limited`/`failed` 三者互斥且相加等于 `requests`：429 单独成列
    是本脚本的核心口径——爬坡的全部意义就是找它的拐点，把它和网络抖动、
    schema 错误混进一个 `failed` 里，这份数据就白跑了。
    """

    concurrency: int
    requests: int
    ok: int
    rate_limited: int
    failed: int
    wall_s: float
    p50_ms: float
    p95_ms: float
    peak_in_flight: int

    @property
    def throughput(self) -> float:
        """**有效**吞吐：只算成功的调用。

        用总请求数算会让"高并发下大批 429 秒回"看起来像吞吐暴涨——那正是
        我们要识别出来的失败模式，不能让它伪装成优点。
        """
        return self.ok / self.wall_s if self.wall_s > 0 else 0.0


def _is_rate_limited(exc: BaseException) -> bool:
    """429 的识别刻意做成鸭子类型。

    anthropic SDK 有 `RateLimitError`，但本脚本的 caller 是注入的，可能来自
    任何 SDK、任何版本；有的把状态码挂在 `status_code`，有的只写进消息里。
    这里两条都认，宁可多认也不要把真实的限流漏记成普通失败——漏记会让整条
    爬坡曲线读出相反的结论（"这一档很健康"）。
    """
    if getattr(exc, "status_code", None) == 429:
        return True
    return "429" in str(exc)


async def run_level(
    caller: Callable[[int], None], *, requests: int, concurrency: int
) -> LevelResult:
    """在给定并发上限下发 `requests` 次请求，返回观测结果。

    **必须用 `asyncio.to_thread` 而不是让 caller 变成协程**：这是在复刻生产
    路径（`graph_fanout` 的 `_bounded_extract_one`/`_bounded_review_one`
    → `await asyncio.to_thread(...)` → 阻塞的 anthropic SDK 调用）。换成纯
    协程会绕开 CPython 默认线程池那道 `min(32, cpu+4)` 上界，测出一条生产
    环境根本达不到的曲线。

    **任何异常都被吞成计数，不向上抛**：整档全挂是数据点不是错误，高档位
    撞墙是爬坡的预期结果，抛出去会让后面的档位跑不到、整轮校准前功尽弃。
    """
    semaphore = asyncio.Semaphore(concurrency)
    latencies: list[float] = []
    counts = {"ok": 0, "rate_limited": 0, "failed": 0}
    in_flight = {"now": 0, "peak": 0}
    lock = threading.Lock()

    async def one(i: int) -> None:
        async with semaphore:
            with lock:
                in_flight["now"] += 1
                in_flight["peak"] = max(in_flight["peak"], in_flight["now"])
            started = time.perf_counter()
            try:
                await asyncio.to_thread(caller, i)
            except BaseException as exc:  # noqa: BLE001 —— 整档全挂也要跑完
                key = "rate_limited" if _is_rate_limited(exc) else "failed"
                counts[key] += 1
            else:
                counts["ok"] += 1
                # 只记成功调用的延迟：失败往往秒回（连接直接被拒），混进来会
                # 把 p95 压得很好看，读表的人会得出"这档很健康"的相反结论。
                latencies.append((time.perf_counter() - started) * 1000)
            finally:
                with lock:
                    in_flight["now"] -= 1

    wall_started = time.perf_counter()
    await asyncio.gather(*(one(i) for i in range(requests)))
    wall = time.perf_counter() - wall_started

    return LevelResult(
        concurrency=concurrency,
        requests=requests,
        ok=counts["ok"],
        rate_limited=counts["rate_limited"],
        failed=counts["failed"],
        wall_s=wall,
        # 没有成功样本时不编造分位数 —— 0.0 在表里配合 ok=0 一起读，
        # 比一个凭空算出来的数字诚实。
        p50_ms=statistics.median(latencies) if latencies else 0.0,
        p95_ms=_percentile(latencies, 95) if latencies else 0.0,
        peak_in_flight=in_flight["peak"],
    )


def _percentile(values: list[float], pct: int) -> float:
    ordered = sorted(values)
    idx = min(len(ordered) - 1, int(round(pct / 100 * len(ordered) + 0.5)) - 1)
    return ordered[idx]


def recommend(results: Sequence[LevelResult]) -> int:
    """从爬坡数据里选默认并发值：**吞吐达到峰值 95% 的最小档位**。

    为什么不是"吞吐最高的档位"：并发不是免费的。多一路在飞的请求就多一份
    账户级配额占用（DeepSeek 的限制正是账户级并发数，同一账户上跑别的活会
    互相挤）、多一份下游限流风险、以及——按 `graph.py` 的 I1 结论——崩溃时
    多一份要白烧的在途调用量。8→16 只换来 3% 吞吐时，那 3% 不值多一倍并发。

    **撞到 429 的档位一律出局**，哪怕吞吐最高：429 意味着已经越界，此时的
    吞吐是"侥幸挤进去的那部分"，换个时段（账户上有别的负载）就不可复现。

    全线撞墙时返回 1 —— 不返回 None 让调用方去猜，也不返回一个已知会越界的
    档位。
    """
    if not results:
        raise ValueError("没有任何观测结果，无法给出推荐值")
    clean = [r for r in results if r.rate_limited == 0]
    if not clean:
        return 1
    best = max(r.throughput for r in clean)
    good_enough = [r for r in clean if r.throughput >= 0.95 * best]
    return min(r.concurrency for r in good_enough)


def format_table(results: Sequence[LevelResult]) -> str:
    header = (
        f"{'并发':>4} {'请求':>4} {'成功':>4} {'429':>4} {'其他失败':>8} "
        f"{'墙钟s':>7} {'吞吐/s':>7} {'p50ms':>7} {'p95ms':>7} {'实测峰值':>8}"
    )
    rows = [
        f"{r.concurrency:>4} {r.requests:>4} {r.ok:>4} {r.rate_limited:>4} "
        f"{r.failed:>8} {r.wall_s:>7.2f} {r.throughput:>7.2f} "
        f"{r.p50_ms:>7.0f} {r.p95_ms:>7.0f} {r.peak_in_flight:>8}"
        for r in results
    ]
    return "\n".join([header, "-" * len(header), *rows])


def _build_real_caller(model: str) -> Callable[[int], None]:
    """真实调用走**生产同款**的 judge，不是自己拼一个 HTTP 请求。

    校准的对象是"这条流水线在这个 provider 上能开多大并发"，不是"这条网络
    链路的极限带宽"。用生产同款的 `DeepSeekJudge`（强制工具调用 + thinking
    disabled + temperature 0）才能把 SDK 的连接池行为、请求大小、服务端处理
    时间都算进去。每个请求内容不同（带序号），避免撞上任何服务端缓存把
    延迟测成假的。
    """
    from cn_curriculum_graph.judges.deepseek_judge import DeepSeekJudge

    judge = DeepSeekJudge(model=model)

    def call(i: int) -> None:
        judge(name=f"三角形内角和（校准样本 {i}）", description="三角形三个内角的和是 180 度")

    return call


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="calibrate-concurrency",
        description="对真实 provider 做并发爬坡，为 --max-concurrency 定默认值",
    )
    parser.add_argument(
        "--levels",
        default=",".join(str(x) for x in DEFAULT_LEVELS),
        help="逗号分隔的并发档位；默认 1,2,4,8,16,32",
    )
    parser.add_argument("--requests", type=int, default=DEFAULT_REQUESTS_PER_LEVEL)
    parser.add_argument("--model", default="deepseek-v4-flash")
    args = parser.parse_args(argv)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("需要 DEEPSEEK_API_KEY（export 或写进 .env）")

    levels = [int(x) for x in args.levels.split(",") if x.strip()]
    caller = _build_real_caller(args.model)

    cpu = os.cpu_count() or 1
    thread_cap = min(32, cpu + 4)
    print(f"模型={args.model}  每档 {args.requests} 次请求")
    print(
        f"本机 asyncio.to_thread 默认线程池上界 = min(32, {cpu}+4) = {thread_cap}"
        "  —— 超过这个数的并发设置不会带来更多真并发\n"
    )

    results: list[LevelResult] = []
    for level in levels:
        # 逐档打印而不是攒到最后：爬坡到高档位可能很慢（撞墙时更慢），
        # 边跑边出数字才看得出该不该提前 Ctrl-C。
        result = asyncio.run(run_level(caller, requests=args.requests, concurrency=level))
        results.append(result)
        print(format_table(results).splitlines()[-1] if len(results) > 1 else format_table(results))

    print("\n" + format_table(results))
    print(f"\n推荐默认值（吞吐达峰值 95% 的最小档位，且该档零 429）：{recommend(results)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
