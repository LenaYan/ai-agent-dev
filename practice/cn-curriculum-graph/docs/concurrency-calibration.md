# 并发上限校准（实测）

> 目的：把 `graph_fanout.DEFAULT_MAX_CONCURRENT_LLM_CALLS` 从"未校准的工程判断"
> 换成有依据的数字。
> 时间：2026-07-27。脚本：`scripts/calibrate_concurrency.py`。
> 环境：macOS，15 核，Python 3.12.13，`deepseek-v4-flash`。

## 0. 结论先行

| | 校准前 | 校准后 |
|---|---|---|
| 默认值 | `8`（魔数） | `thread_pool_ceiling()` = `min(32, cpu_count+4)`，本机 **19** |
| 依据 | "保守起点，未做 provider 端实测" | 全档爬坡实测 + provider 官方文档 |
| 认知 | "8 保守是为了不撞 provider 限流" | **provider 根本不是瓶颈**，瓶颈在本机线程池 |

一句话：**原来那个 8 保守的是一个根本不紧的约束。**

## 1. 先查文档：DeepSeek 的限制不是 RPM/TPM

<https://api-docs.deepseek.com/quick_start/rate_limit>（2026-07-27 核实）：

- **不设 RPM/TPM 配额**。官方原文是"不限制用户的速率"，服务端繁忙时的策略是
  **拖慢**而不是拒绝（非流式回空行、流式回 `: keep-alive` 注释保活，10 分钟
  还没开始推理才断连接）。
- 设的是**账户级并发连接数**上限：`deepseek-v4-flash` **2500**、
  `deepseek-v4-pro` **500**。一个请求从发出到响应完成期间占一个连接；
  上限按**账户**算，与用了几个 API key 无关。超出返 HTTP 429。

**第一个认知修正**：默认值 8 比 flash 的配额低了两个数量级（300 倍）。原注释
说"选一个留出安全边际的保守起点"——安全边际是留了，但留在了一个不紧的约束上。

## 2. 三道上界，只有一道是 provider 给的

| 上界 | 数值 | 谁定的 |
|---|---|---|
| 账户级并发连接 | 2500 (flash) / 500 (pro) | DeepSeek |
| **`asyncio.to_thread` 默认线程池** | **`min(32, cpu_count + 4)`**，本机 19 | CPython |
| 我们的信号量 | `DEFAULT_MAX_CONCURRENT_LLM_CALLS` | 本项目 |

中间那道最容易漏，而它才是真正卡住的那道：

流水线里每一次 LLM 调用都走 `await asyncio.to_thread(...)`。这不是风格选择
——它是为了让 `NODE_TIMEOUT` 真正生效被逼出来的（`graph.py` 的 C1 修复记录：
`async def` 但函数体不 `await` 会把事件循环整个占住，看门狗拿不到调度）。
而 `asyncio.to_thread` 用的是事件循环的**默认 executor**，
`ThreadPoolExecutor` 的默认 `max_workers = min(32, os.cpu_count() + 4)`。

**这条约束是我们自己引入的**——为了拿到超时能力，付出了一个并发天花板。
两者是同一个决定的两面，在这次校准之前从没有被联系起来看过。

## 3. 爬坡实测

方法：用**生产同款**的 `DeepSeekJudge`（强制工具调用、`thinking=disabled`、
`temperature=0`）发请求，走**阻塞函数 + `asyncio.to_thread` + 信号量**，
完全复刻 `graph_fanout` 的执行路径。每个请求内容带序号，避免撞服务端缓存。

> 用 `await asyncio.sleep` 那种纯协程写法会绕开线程池，测出一条生产环境
> 根本达不到的假曲线——这是这份数据能不能用的前提。

### 第一轮：粗扫 1→32，每档 20 次

```
并发  请求  成功  429  其他失败   墙钟s  吞吐/s  p50ms  p95ms  实测峰值
  1    20    20    0       0     25.11   0.80   1235   1536      1
  2    20    20    0       0     11.71   1.71   1129   1353      2
  4    20    20    0       0      5.84   3.43   1136   1369      4
  8    20    20    0       0      3.70   5.41   1197   1418      8
 16    20    20    0       0      2.23   8.96   1141   1231     16
 32    20    20    0       0      2.22   9.02   1259   2204     20   ← 峰值只有 20
```

**注意最后一行**：并发设 32，`实测峰值` 只有 **20**。线程池天花板当场现形
（`min(32, 15+4) = 19`，计数器上有一个采样竞态所以显示 20）。同时 p95 从
1231ms 跳到 2204ms——多出来的请求在排队，不在飞。

### 第二轮：细化拐点 8→24，每档 40 次

```
并发  请求  成功  429  其他失败   墙钟s  吞吐/s  p50ms  p95ms  实测峰值
  8    40    40    0       0      6.17   6.49   1122   1335      8
 12    40    40    0       0      4.50   8.90   1095   1285     12
 16    40    40    0       0      3.74  10.69   1227   1458     16
 19    40    40    0       0      3.17  12.60   1090   1697     19   ← 拐点
 24    40    40    0       0      3.31  12.09   1162   2143     24
```

**全程 0 个 429**——和官方文档给的 2500 配额一致，我们离它还差两个数量级。

**吞吐拐点正好压在线程池天花板上**：8→19 一路涨（6.49 → 12.60 req/s，
**+94%**），再往上到 24 反而回落到 12.09（−4%），而 p95 从 1458ms 涨到
2143ms（+47%）。这是教科书式的排队现象：超过服务台数量之后，加请求只加
等待时间。

## 4. 定默认值

选取规则（`recommend()`，写成函数是为了让判断可测、可复核）：
**取吞吐达到峰值 95% 的最小档位，且该档零 429**。

- 不选"吞吐最高的档位"：并发不是免费的。多一路在飞的请求 = 多一份账户级
  配额占用（账户级意味着同账户上跑别的活会互相挤）+ 多一份下游限流风险 +
  按 `graph.py` 的 I1 结论，崩溃时多一份要白烧的在途调用量。
- 撞过 429 的档位一律出局，哪怕吞吐最高：那时的吞吐是"侥幸挤进去的那部分"，
  换个时段就不可复现。

两轮数据都指向 19 = `min(32, cpu_count+4)`。

**默认值写成公式 `thread_pool_ceiling()` 而不是常量 19**：19 是这台机器的
cpu 数算出来的，抄下来换台机器就又变回一个没有依据的魔数了。

## 5. 适用边界（这个数字什么时候不对）

1. **同进程还有别的 `asyncio.to_thread` 使用者** → 应调低，默认线程池是共享的。
2. **同账户同时跑别的 DeepSeek 负载** → 应调低，2500/500 是账户级共享配额。
3. **换 provider** → 重跑一次 `scripts/calibrate_concurrency.py` 即可，
   别的 provider（OpenAI/Anthropic）用的是 RPM/TPM 而不是并发连接数，
   拐点形态会完全不同。
4. **想调到天花板以上** → 必须先 `loop.set_default_executor()` 换一个更大的
   `ThreadPoolExecutor`，否则信号量放行再多也只是排队。
   `build_fanout_graph` 会为这种设置发一条 `RuntimeWarning`——不静默截断，
   否则现象是"我明明调到 64 了怎么没变快"而代码里没人告诉你原因。

## 6. 顺带补上的生产入口

校准出一个默认值，却发现 **`--engine` 根本没有扇出版这个选项**——
`max_concurrency` 此前只有 `scripts/compare_orchestration.py` 和测试碰得到。
一个校准好的参数如果没有生产路径能用上，校准就只是自娱自乐。

于是补了 `--engine langgraph-fanout` 与 `--max-concurrency`
（`tests/pipeline/test_run_cli.py`），并让 `--max-concurrency` 配另外两个引擎时
**当场退出而不是静默忽略**（它们没有扇出点，静默会让人建立"我限住了并发"
的错误预期）。

真实端到端验证（3 条课标 → 4 节点 1 边，0 error，55s）：

```
uv run python -m cn_curriculum_graph.pipeline.run \
    --engine langgraph-fanout --source data/source --out data/generated \
    --checkpoint data/cp.sqlite
```

## 7. 复现

```bash
DEEPSEEK_API_KEY=... uv run python scripts/calibrate_concurrency.py \
    --levels 1,2,4,8,16,32 --requests 20 --model deepseek-v4-flash
```

成本：每次请求约 100 token 输入 / 十几 token 输出。两轮共 320 次调用，
flash 上远小于 $0.01。
