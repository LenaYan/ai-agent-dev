# 手写编排 vs LangGraph：一次受控实验的对比笔记

> 这是路线图"阶段四·主流框架"里 LangGraph 分支的验收物。结论只对本项目
> 这条六层纯函数流水线成立，不是对 LangGraph 的通用评价。
>
> **三条必须先知道的限制**（贯穿全文，不在每个数字后面重复）：
>
> 1. 实验用 fake 而非真模型（`scripts/compare_orchestration.py` 的
>    `_fake_deps`），测不出真实限流行为——真实 429 常伴 `Retry-After` 头、
>    并发退避、抖动，这些在 fake 实验里完全不存在。
> 2. 自定义类型（`Chunk`、`TopicDraft`）进 checkpoint 需注册
>    `allowed_msgpack_modules`，本项目至今没有注册，未来版本会强制拦截
>    （见下文"框架强加了什么"第 5 条）。
> 3. LangGraph 1.2 发布于 2026-05，API 仍在快速变化。本文数据基于
>    **1.2.9**（`docs/langgraph-comparison-design.md` §2 记录的实测版本）。

## 第一章：对等对比（A 阶段：手写版 vs LangGraph 版）

A 阶段是同一份六层纯函数流水线的两种编排实现，测试对实现无感知
（`tests/pipeline/test_run.py` 的 13 条端到端用例两边参数化跑，全绿），
这是"对等"这个说法的定义。

### 数据表：三个规模的受控实验

跑法：`uv run python scripts/compare_orchestration.py --chunks {3,10,20}`
（`judges_per_category` 用默认值 2，对齐 `run.py` CLI 生产默认）。

**n=3**

| 场景 | 引擎 | 首轮崩溃 | 总调用 | 恢复重跑 | 耗时(s) |
|---|---|---|---|---|---|
| baseline | handwritten | 否 | 24 | 0 | 0.0 |
| baseline | langgraph | 否 | 24 | 0 | 0.01 |
| hard_crash | handwritten | 是 | 27 | 24 | 0.0 |
| hard_crash | langgraph | 是 | 24 | 21 | ~2.6（jitter，见下） |
| partial_crash | handwritten | 是 | 28 | 24 | 0.0 |
| partial_crash | langgraph | 是 | 27 | 21 | ~2.8 |

**n=10**

| 场景 | 引擎 | 总调用 | 恢复重跑 |
|---|---|---|---|
| baseline | handwritten / langgraph | 129 / 129 | 0 / 0 |
| hard_crash | handwritten / langgraph | 143 / 129 | 129 / 115 |
| partial_crash | handwritten / langgraph | 147 / 141 | 129 / 115 |

**n=20**

| 场景 | 引擎 | 总调用 | 恢复重跑 |
|---|---|---|---|
| baseline | handwritten / langgraph | 409 / 409 | 0 / 0 |
| hard_crash | handwritten / langgraph | 453 / 409 | 409 / 365 |
| partial_crash | handwritten / langgraph | 462 / 436 | 409 / 365 |

（完整原文输出见本次实验记录，三个规模逐字复现了 Task 6 报告的数字，
说明这套实验装置是可复现的，不是偶然跑出来的一组数字。）

### 框架省了什么

**checkpoint 断点续跑**：收益 = 崩溃点上游全部调用量，公式
`gap = N_chunks + P`（`P` 是 dedupe 候选对数，因为 `dedupe.candidate_pairs`
是 O(N²) 扫描，`P` 随 chunk 数**超线性**增长，`gap` 也就跟着超线性放大）：

| n（chunks） | hard_crash 净优势（`总调用hw - 总调用lg`） | partial_crash 净优势 |
|---|---|---|
| 3 | 3 | 1 |
| 10 | 14 | 6 |
| 20 | 44 | 26 |

**但这两列不能只看 `hard_crash` 那一列就下结论。** `hard_crash` 的故障
（`_boom_propose_all`）注在 `edges_mod.propose_all` 的**入口处**——重试的
3 次尝试全部是零调用，重试代价被完全隐藏，`hard_crash` 净优势因此是一个
**上界**，虚抬了 LangGraph 的实际收益。`partial_crash`（`_partial_boom_
propose_all`）先真实处理完"前一半有候选池的 target"再抛出，让每次重试
尝试都真实烧调用——这才是更接近真实故障的数字，净优势因此在三个规模下
都显著更小（3→1，14→6，44→26，约缩水 2/3 到 3/5）。**读这张表时，
`partial_crash` 一列才是该引用的数字，`hard_crash` 一列只用来说明
"故障发生的精确时机会让净优势的表观数字剧烈波动"这件事本身。**

### 框架强加了什么

**1. Node 级 `RetryPolicy` 对"LLM 偶发失败"这一类最常见的真实故障不可达。**
`extract_all`/`dedupe`/`propose_all`/`review_drafts`/`review_edges` 六层
纯函数一律"逐条 `try/except`：非程序错误转成 `DropRecord`、`continue`"
（`src/cn_curriculum_graph/pipeline/extract.py` 的 `extract_all` 即是
一例），故障永远不会从这层函数本身冒泡出去，`RetryPolicy` 从来等不到
触发的机会。**而它一旦真能触发，粒度是整层，不是整条**——实测：4 个
chunk、故障注在 `extract_all` **这次整批调用**的最后一个 chunk 时才抛出
（模拟"处理到一半才崩"）：第一轮 4 次调用（全部执行完、最后一条才失败），
resume 后**全部 4 个 chunk 的 extractor 又重新调用一遍**，两轮合计 8 次，
对照无故障基线 4 次——手写版逐条 `try/except` 在这个粒度上严格更优：
它天然是"单条失败不影响其它条"，根本不需要"整层重跑"这个概念。

**2. `NODE_TIMEOUT` 只改变返回结果，不提供墙钟上界（这条此前的估算数字
是错的，以下是实测的真实数字）。** `NODE_TIMEOUT=1s`、抽取层阻塞
`sleep=3s`（`tests/pipeline/test_graph.py::test_node_timeout_does_not_
provide_a_wall_clock_bound`）：`NodeTimeoutError` 如期在约 1.000s 抛出
（`test_graph.py` 另一条用例断言异常文本含 `"run timeout of 1.000s"`），
但 `run_pipeline_lg` 本身的墙钟耗时贴近 `sleep_seconds`（断言
`elapsed >= sleep_seconds * 0.9`，即 ≥2.7s，实测约 3.01s）——`timeout`
拿到的只是"提前放弃、返回异常"，被超时判定放弃的那条后台线程仍在事件
循环之外真实跑到底。根因：`asyncio.run` 收尾时 CPython
`asyncio.runners.Runner.close()` 会
`run_until_complete(loop.shutdown_default_executor(THREAD_JOIN_TIMEOUT))`
去 join 那条杀不掉的线程，`THREAD_JOIN_TIMEOUT = 300`——这个 300 秒是
读 CPython 标准库源码得到的常量，本项目没有专门跑满 300 秒去实测这个
上限本身，只实测验证了"阻塞时长贴近真实调用耗时、明显超过 NODE_TIMEOUT"
这个方向。

**3. checkpoint 重入逼你把持久化做成幂等的**——手写版从不需要付这个成本。
`run_pipeline_lg` 的 S1 修复（`_ensure_consistent_resume`）就是这个代价
的直接体现：resume 前必须校验 `source_dir`/`out_dir`/`model_id`/
`curriculum` 四个字段与 checkpoint 里存的是否一致，不一致就拒绝静默复用
旧参数（`src/cn_curriculum_graph/pipeline/graph.py:558` 起）。这段代码
（连同 `run_pipeline_lg` 本身）在手写版里完全不存在，因为手写版没有"续跑"
这个概念，也就没有"续跑时参数对不上怎么办"这个问题需要解决。

**4. `retry_on` 会重试 `ValueError`，而项目刻意的"配置错误当场炸"哨兵
全用 `ValueError`。** 修复前实测：空 `judges` 时 LangGraph 把同一个
确定性错误犯了 3 遍（`review_drafts` 被调用 3 次而不是 1 次，见
`.superpowers/sdd/task-5-report.md` 的 RED 记录）——`RetryPolicy` 默认
不区分"这是一次性配置错误，重试 3 次只会用同样的坏配置再犯 3 次同样的
错"和"这是一次瞬时网络故障，重试可能自愈"。已修复为 `retry_on` 显式排除
`ValueError`（`graph.py:115` 起），修复后同一测试断言调用次数恢复为 1。

**5. msgpack 自定义类型未注册**：checkpoint 反序列化 `Chunk`/`TopicDraft`
时走的是兜底路径，实测运行时打印的原文警告：

```
Deserializing unregistered type cn_curriculum_graph.pipeline.models.Chunk from checkpoint. This will be blocked in a future version. Set LANGGRAPH_STRICT_MSGPACK=true to block now, or add to allowed_msgpack_modules to allow explicitly: [('cn_curriculum_graph.pipeline.models', 'Chunk')]
```

`LANGGRAPH_STRICT_MSGPACK=true` 或未来版本默认收紧后，这条路径会直接
失效——本项目至今没有注册 `allowed_msgpack_modules`，这是真实存在、
尚未处理的技术债，不是理论风险。

**6. 异步传染。** 为让 `NODE_TIMEOUT` 真正生效，四个走 LLM 的 Node 必须
是 `async def` 且函数体真的 `await asyncio.to_thread(...)`（光有
`async def` 不够，见上面第 2 条）；checkpointer 必须从 `SqliteSaver` 换成
`AsyncSqliteSaver`；`run_pipeline_lg` 内部用 `asyncio.run(...)` 包一层
`.ainvoke(...)`，这意味着**它不能从任何已有事件循环的上下文里调用**——
`graph.py` 里 `run_pipeline_lg` 的 docstring 已预警了这一点，调用方若从
一个 `async def` 函数内部或已运行的 asyncio server 调用它会得到
`RuntimeError: asyncio.run() cannot be called from a running event loop`
（这句异常文本是文档作者对 Python 标准库已知行为的复述，不是本项目
某条测试实际触发并断言过的输出——本项目当前的 CLI 和 pytest 用例都是
同步调用，没有踩到这条限制，但这是留给未来"把流水线包进已有 async
上下文"场景的真实约束）。

**7. 代码量。** 用 Task 6 建立的口径（非空、非注释、非 docstring 正文行，
两边同一把尺子）重新核对：手写版 `run_pipeline` 函数体 **84** 行
（`run.py:61-184`）；LangGraph 版编排机制（`PipelineState` 定义 + 六个
Node 函数体 + `build_graph`，不含断点续跑相关代码）**139** 行——**1.65×**；
把断点续跑相关代码（`run_pipeline_lg` 本身 + `_ensure_consistent_resume`
+ 相关模块级常量）也计入，**192** 行——**约 2.3×**。（这次重新核对是因为
`graph.py` 在 Task 7 之后插入了新注释，原 Task 6 报告引用的具体行号区间
已经偏移；用当前文件的实际函数边界重新数出的合计数字与 Task 6 报告的
139/84 完全一致，说明代码本身的规模没有变化，只是行号漂移了。）

### 一条正面素材

LangGraph 只把最终成功那次调用的返回值当作该 step 的 delta，失败的
重试尝试不产生 delta——所以 reducer 累加语义在重试下不会翻倍，这件事
框架帮你兜住了。**这条是我对 LangGraph Pregel 执行模型的判断**（重试
是重新调用整个 Node 函数，只有不抛异常那次的返回值会被记为该 step 的
delta），本项目没有专门写测试去实测验证这个机制本身，不是"实测表明"。

## 第二章：额外解锁（B 阶段：`Send` 扇出到条目级）

> **声明：B 阶段已不是"同一需求的两种实现"——架构变了。** 它回答的是
> 另一个问题："框架让我做成了原本不会去写的事吗，收益多大？"
> `graph_fanout.py` 没有接入 `test_run.py` 那 13 条两引擎对等的参数化
> 端到端测试，这个数据不能跟第一章的表格混着读。

### 先说一个诚实的、反直觉的发现：这次实验没有测出 B 阶段的优势

把 `fanout` 引擎接入 `scripts/compare_orchestration.py`（`_invoke` 新增
分支调 `graph_fanout.run_pipeline_fanout`），复用第一章**同一组场景**
跑 n=3/10/20，三个规模里 `fanout` 与 `langgraph` 的 `总调用`/`恢复重跑`
**逐位相同**：

```
# n=10（三个规模的模式完全一致，这里只贴一个）
hard_crash        langgraph     ... 总调用=129 恢复重跑=115 ...
hard_crash        fanout        ... 总调用=129 恢复重跑=115 ...
partial_crash     langgraph     ... 总调用=141 恢复重跑=115 ...
partial_crash     fanout        ... 总调用=141 恢复重跑=115 ...
```

原因：`hard_crash`/`partial_crash` 的故障注在 `edges_mod.propose_all`，
而 `edges` 这一层在 B 阶段**没有被扇出**（`dedupe`/`edges` 需要全局视野，
`graph_fanout.py` 顶部文档明确说明"扇不开"）。故障没有命中被扇出的层，
B 阶段的条目级 checkpoint 就无从体现优势——`fanout` 在这组数据里退化成
和 `langgraph` 完全一样的行为。**这本身就是第二章要传达的核心结论之一：
B 阶段的收益不是自动的，只在故障命中被扇出的节点（`extract_one`/
`review_one`）内部时才会体现；换一个故障位置，B 阶段可能什么都不多给。**

### 真正命中扇出层时的数字（自己构造的对照实验）

为了量出"故障命中扇出层"这个真正的对照点，构造了一个新实验：4 个
互不相似的知识点，`extract_mod.extract_all` 第一次处理第 2 条 chunk
时失败一次，之后正常，`RETRY_POLICY` 调成 `max_attempts=1`（排除 Node
内部重试自愈的干扰，逼真相只能来自 checkpoint resume）。

**fanout（B 阶段，条目级扇出）**：

```
第一轮各 chunk 调用次数：{'1.1.1': 1, '2.2.1': 1, '3.3.1': 1, '4.4.1': 1}
第二轮（resume）各 chunk 调用次数：{'2.2.1': 1}
```

resume 后**只有失败过的那一条被重新调用**，其余 3 条完全不出现在第二轮
调用记录里（0 次）。

**langgraph（A 阶段，extract 是单个 Node，一次调用处理全部 4 个 chunk）**：
同样"故障命中被扇出层"这件事在 A 阶段没有对应物——A 阶段的 extract
本来就是整批一次调用，不存在"扇出层"这个概念，能观察到的只是"整层是否
完成"。用上一章"框架强加了什么"第 1 条的同一份实测数据作对照：4 个
chunk、故障在这次整批调用的**最后一条**才触发，resume 后**全部 4 个
chunk 都被重新调用**，两轮合计 8 次调用（无故障基线 4 次）。

**并列对照**：同样是"1 条崩溃、其余 3 条已经处理成功"这个情形，
B 阶段的条目级 checkpoint 只需要在 resume 时补 1 次调用（净成本 = 1，
其余 3 条 0 成本）；A 阶段无论是手写版还是 LangGraph 版，extract 都是
一次批处理调用，resume（或手写版的整体重跑）都要把全部 4 条重新算一遍
（净成本 = 4）。**这是 B 阶段唯一有意义、且被独立测试反复验证过的
真实收益**：`tests/pipeline/test_graph_fanout.py::test_item_level_
checkpoint_only_reruns_the_failed_chunk` 用另一份更小的 2-chunk fixture
（`3.1.1`/`3.1.2`）独立验证了同一件事——resume 后从未失败过的 `3.1.1`
调用次数为 0，失败过的 `3.1.2` 被重新调用 1 次，这条测试目前在全量
233 个用例里持续通过，不是一次性观察。

### 这个收益要用什么代价换

第一章"框架强加了什么"的全部 7 条（Node 级重试不可达、`NODE_TIMEOUT`
不提供墙钟上界、幂等化要求、`retry_on`/`ValueError`、msgpack 未注册、
异步传染、代码量 1.65×/2.3×）B 阶段一条都没有减免，只是在此之上又多
引入了 `FanoutState` 子类、`Send` 扇出源为空列表时的兜底路由（已实测
踩坑：零个 `Send` 任务等价于下游收敛节点完全不触发，`assemble` 跑不到，
`run_pipeline_fanout` 取 `result["findings"]` 直接 `KeyError`，见
`graph_fanout.py` 顶部文档）这类扇出特有的复杂度。

## 第三章：什么时候不该用它

以下结论全部由前两章的实测数据支撑，不是脱离数据的主观断言。

**1. 如果故障主要是"LLM 偶发失败"这一类最常见的真实故障，别用它。**
第一章已证：六层纯函数的逐条 `try/except` 会把这类故障吞成 `DropRecord`，
`RetryPolicy` 从不可达（`judge_exception_swallowed` 场景，三个规模下
`handwritten`/`langgraph`/`fanout` 三个引擎的 `总调用`/`恢复重跑`
**逐位相同**，没有任何差异）。这种情况下引入 LangGraph 只换来 1.65×
（或 2.3×，如果还要用断点续跑）的代码量、msgpack 风险、异步传染，
换不到任何实际收益。

**2. 如果流水线是一串"批处理式"的层（一次函数调用处理一批条目，像本项目
的 `extract_all`/`dedupe`/`propose_all`），checkpoint 收益上限是
"崩溃点之前的全部层"，且这个上限的精确大小高度依赖"故障发生在这次批处理
调用的哪个位置"——`hard_crash`（入口即崩，重试零成本）与 `partial_crash`
（处理到一半才崩，重试真烧调用）在完全相同的 chunk 规模下，净优势能相差
2/3 到 3/5（n=20 时从 44 掉到 26）。如果不能预先知道故障通常发生在层的
哪个阶段，这个收益数字本身就是不稳定的，不该被当成一个可以提前承诺的
"节省百分之多少调用"的量化指标。

**3. 只有当故障发生位置精确落在被扇出的条目级节点内部、且条目数量本身
大到"整批重算"是真实业务成本时，B 阶段这种条目级 checkpoint 才有实打实
的收益**（第二章：4 个条目里 1 个失败，B 阶段净成本 1，A 阶段净成本 4）。
但这个收益不是免费的：得先把对应的层从"一次批处理调用"重构成"逐条目
`Send` 扇出 + 收敛节点"，这本身是一次不小的架构改动（`graph_fanout.py`
比 A 阶段的 `graph.py` 多出一整套 `FanoutState`、扇出路由、空列表兜底
逻辑），而且第一章的全部代价（1）-（7）在 B 阶段一条都没有减免。

**4. 如果团队已经有一套跑得动、能落盘检查、能写跨层守恒断言的纯函数
流水线（本项目 159→233 个测试的路子），引入 LangGraph 的决策不该基于
"框架应该会让恢复更省事"这种直觉——第一章的 `hard_crash` vs
`partial_crash` 对照已经证明，同一个"框架帮你省调用"的直觉印象，换一个
故障注入的精确时机，数字可以缩水到只剩 1/3 左右。量化承诺前应该先用
类似本文的受控实验，在自己的真实故障分布下测一遍，而不是照抄任何一篇
对比笔记（包括这一篇）的具体倍数。

## 数据口径的诚实声明

- 合成源文本的 `P`（dedupe 候选对数）依赖 `_TOPIC_POOL` 的名称分布
  （6 个核心概念名循环取值），这是模拟"知识点跨年级复现"的简化方案，
  真实课标里 `P` 的实际分布需要用真实语料另测，本文的曲线只能说明
  "候选对数会随 chunk 数增长"这个方向，具体数值不是对真实课标的预测。
- `seconds` 列受 `RetryPolicy` 的 `jitter=True` 影响明显不稳定——本次
  n=3/10/20 三轮实际跑出的 `hard_crash`/`partial_crash` 的
  `langgraph`/`fanout` 耗时分布在 2.05s~3.35s 之间（例如 n=3 的
  `hard_crash langgraph`=2.91s 而同一场景 `fanout`=2.58s，n=10 的
  `hard_crash fanout`=3.35s 而 `partial_crash fanout`=2.05s），同一份
  代码、同一个场景，量级都在 2~3s 上下浮动，本文只引用量级，不引用
  具体小数位。
