"""LLM 调用的并发上限 —— 单一事实来源。

**为什么单独成一个模块**：这个上限有两个使用者，分属不同层：

- `graph_fanout.py`（编排层）：`extract_one` / `review_one` 两个扇出点的信号量
- `review.py`（六层纯函数之一）：`review_edges` 内部的线程池

让 `review.py` 反过来 import `graph_fanout.py` 会把纯函数层拴在编排层上 ——
而"六层纯函数不依赖任何编排实现"正是手写 vs LangGraph 那轮对比能成立的前提
（见 `docs/langgraph-vs-handwritten.md`）。所以常量放中立位置，两边都 import 它。

校准过程与数据见 `docs/concurrency-calibration.md`。
"""

from __future__ import annotations

import os


def thread_pool_ceiling() -> int:
    """`asyncio.to_thread` 在本机能提供的真并发上界。

    CPython 的 `asyncio.to_thread` 用的是事件循环的**默认 executor**，即一个
    `max_workers = min(32, os.cpu_count() + 4)` 的 `ThreadPoolExecutor`。
    流水线里每一次 LLM 调用都走 `await asyncio.to_thread(...)`（那不是风格
    选择，是为了让 `NODE_TIMEOUT` 真正生效被逼出来的，见 `graph.py` 的 C1
    修复记录），所以这个数字就是"同一时刻真正能有几个请求在飞"的硬上界。

    做成函数而不是模块级常量：`os.cpu_count()` 在容器里会随 cgroup 配额变，
    调用时算才拿得到当时的真值。
    """
    return min(32, (os.cpu_count() or 1) + 4)


DEFAULT_MAX_CONCURRENT_LLM_CALLS = thread_pool_ceiling()
"""同一时刻允许同时在飞的 LLM 调用数上限。

**默认值的由来（2026-07-27 实测校准）**——完整数据见
`docs/concurrency-calibration.md`、脚本见 `scripts/calibrate_concurrency.py`：

1. **provider 侧根本不是瓶颈。** DeepSeek 给的不是 RPM/TPM，而是账户级并发
   连接数：`deepseek-v4-flash` 2500、`deepseek-v4-pro` 500，超出返 429。
   1→32 全档爬坡实测 **0 个 429**。此前那个未校准的默认值 8 比 flash 的配额
   低了两个数量级 —— "保守"这个词当初用错了地方。
2. **真正的天花板在本机，就是 `thread_pool_ceiling()`。** 并发设 32 时实测
   在飞峰值只有 20（`min(32, 15+4)=19` 那台机器）。
3. **吞吐拐点正好压在这道天花板上。** 40 次请求/档：8→6.49 req/s、12→8.90、
   16→10.69、19→**12.60**、24→12.09（回落），p95 同时从 1458ms 涨到 2143ms。

**`review_edges` 用的是自己的线程池，不是默认 executor** —— 所以它不受
`thread_pool_ceiling()` 物理约束，这个值对它是**策略上限**而非物理上限。
两者取同一个数是刻意的：真实运行里这两处并发**时间上不重叠**（边审核发生在
draft 审核收敛之后），共用一个数不会叠加抢占额度，也省得维护两套。
"""
