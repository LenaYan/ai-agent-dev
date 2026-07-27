# 手写编排 vs LangGraph：一次受控实验的对比笔记

> 这是路线图"阶段四·主流框架"里 LangGraph 分支的验收物。结论只对本项目
> 这条六层纯函数流水线成立，不是对 LangGraph 的通用评价。
>
> **三条必须先知道的限制**（贯穿全文，不在每个数字后面重复）：
>
> 1. 实验用 fake 而非真模型（`scripts/compare_orchestration.py` 的
>    `_fake_deps`），测不出真实限流行为——真实 429 常伴 `Retry-After` 头、
>    并发退避、抖动，这些在 fake 实验里完全不存在。
> 2. ~~自定义类型（`Chunk`、`TopicDraft`）进 checkpoint 需注册
>    `allowed_msgpack_modules`，本项目至今没有注册，未来版本会强制拦截。~~
>    **这条判断已被实测推翻并改写，见"框架强加了什么"第 5 条**——框架自己
>    会从 state schema 推导 allowlist，真正的坑在别处（而且更隐蔽）。
> 3. LangGraph 1.2 发布于 2026-05，API 仍在快速变化。本文数据基于
>    **1.2.9**（`docs/langgraph-comparison-design.md` §2 记录的实测版本）。

## 第一章：对等对比（A 阶段：手写版 vs LangGraph 版）

A 阶段是同一份六层纯函数流水线的两种编排实现，测试对实现无感知
（`tests/pipeline/test_run.py` 的 14 条端到端用例两边参数化跑，全绿——
用 `grep -c '^@ENGINES' tests/pipeline/test_run.py` 核实过，不是此前
写的 13 条）。**但"全绿"要先收窄一句才配得上"对等"这个定义**：这 14
条用例（连同其中 4 条逐文件逐字节比对两引擎产物的用例：
`test_two_engines_produce_identical_artifacts_on_normal_run` 等）全部
经由 `test_run.py:31` 的 `_run()` 调用 `run_pipeline_lg`，而 `_run()`
**没有传 `checkpoint_db`**（文件里全部 `run_pipeline_lg(...)` 调用点也
都没有）——严格意义上，"对等性测试全绿"这个结论**只对
`checkpoint_db=None`（不启用 checkpointer）这一种模式成立**。这不是
无关紧要的细节：全分支审查发现的 Critical 1（跑完的 checkpoint thread
再跑会 state 累加，见下文"框架强加了什么"第 3 条）**恰好只在
`checkpoint_db` 非空、且同一 thread 第二次被调用时触发**——修复前
233 个测试全绿，正是因为没有一条覆盖"跑完 → 再跑"这个具体状态转换，
而这正是生产 CLI 的默认路径（`derive_thread_id` 按 `source|out` 稳定
派生 thread_id，同一实验重跑会撞上同一个 thread_id）。**这本身就是一条
教训，比任何具体数字都值得记住：我们的对等性测试恰好绕开了框架唯一
有价值的模式（checkpoint 断点续跑）**——"两边跑全绿"验证的是"两套编排
在无持久化状态时行为一致"，不是"两套编排在框架真正发挥断点续跑价值的
路径上行为一致"。

尽管如此，有一条更硬的旁证部分弥补了这个盲区：审查者做过一次**没有
固化为测试、跑完即弃的探针**（代码库里搜不到这组数字，读者需要自行
复现才能验证）——n=6，`hard_crash`/`partial_crash` 两个真正会触发
checkpoint resume 的故障场景，三个引擎（handwritten/langgraph/fanout）
各自崩溃后 resume，逐个比对 8 个落盘文件的字节内容，`DIFFS=0`；无故障
的 baseline（n=10）同样 0 差异，**且 `fanout` 与另两个引擎的产物也
逐字节一致**。这组数字没有对应的 pytest 用例，不应被当成 CI 会一直
守住的结论，但它是这份笔记里对"A 阶段最终产物对等"最直接的一次验证——
比第一章现有的任何论据都更硬地支撑这个结论，只是没有被固化、无法重复
核实，这条局限务必和上一段的"对等只在 checkpoint_db=None 下成立"放在
一起读。

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
`gap = N_chunks + P`（`P` 是 dedupe 候选对数）。重新核实过：`gap = N + P`
与下表 `hard_crash` 净优势逐位相等（n=3/10/20 分别是 3/14/44，反推
`P` = 0/4/24）。`P` 精确等于 `_TOPIC_POOL` 固定的 6 个名字循环取值
产生的同名对数之和 `P = Σ_{k=1}^{6} C(n_k, 2)`（`n_k` 是第 k 个名字
在这 N 个 chunk 里被取到的次数，`n_k ≈ N/6`）；N 是 6 的倍数时化简为
`6·C(N/6,2) = N(N-6)/12`。**`N²/12` 这个系数完全来自"词表固定为 6 个"
这个 fixture 特性，不是 `candidate_pairs` 算法本身的复杂度结论**——
真实课标的知识点词表会随语料规模增长（内容越多、独立概念越多），不会
像这里一样卡死在 6 个名字反复复用，`P` 在真实数据上是否也超线性增长
要另测，本文这条曲线只在"词表固定"这个简化假设下成立。

因果链也要写准：`candidate_pairs` 本身的 O(N²) 扫描**不烧 LLM
调用**——它是纯 Python 字符串比较（`normalize_name` 相等、
`_containment_ratio` 相似度 ≥ 0.85、或 `standard_codes` 有交集，三选
一即进候选，`src/cn_curriculum_graph/pipeline/dedupe.py:51` 起）。真正
烧调用的是扫描后进入候选池、被 `dedupe()` 逐对送进 `judge()` 的那些对
（`dedupe.py:250` 起的循环，每个候选对精确对应一次 LLM 调用）。本实验
里这些候选对**几乎全部来自同名精确匹配，不是相似度阈值**——
`_TOPIC_POOL` 的 6 个名字彼此已用 `_containment_ratio` 逐对验证过都
< 0.85 合并阈值（脚本注释明确写了这点），不会靠"相似"进候选，靠的是
循环取值产生的精确重名。

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

**3. checkpoint 重入逼你把持久化做成幂等的——而且不只是"校验参数"这一层，
它更深一层逼你把整个 state reducer 做成幂等的。** 手写版从不需要付这个
成本。`_ensure_consistent_resume` 只解决了"续跑时参数对不对得上"这一层
（resume 前校验 `source_dir`/`out_dir`/`model_id`/`curriculum` 四个字段
与 checkpoint 里存的是否一致，不一致就拒绝静默复用旧参数，
`src/cn_curriculum_graph/pipeline/graph.py:588` 起——行号因 Critical 1
的修复后移，此前笔记写的 558 是修复前的位置）——但全分支审查发现
的 Critical 1（commit `7c4ddd2`）证明了光这层校验不够：真正的坑在于
`drafts`/`drops`（fanout 版还有 `draft_review_kept`/`draft_review_
outcomes`）全部是 `Annotated[..., operator.add]` 的 reducer 字段，
LangGraph 在**同一 thread 上开新 run 不会重置 channel**——thread 已经
跑完（`existing.next == ()` 且 `existing.values` 非空）之后，同一
thread_id 再调用一次 `ainvoke(payload)`，第二次的 delta 会**累加**在
第一次已完成的值上：dedupe 看到两倍同名草稿、互相当成重名抵消，产出一份
topics 归零的 graph.json，覆盖掉上一次跑出的好图。也就是说，光校验"这次
调用的参数和上次一致"还不够——**上次的 state 本身也得被安全地处理掉**，
否则"参数一致、可以复用"这个正确判断反而会加速状态污染（确认可以复用
之后就真的把两轮数据叠到了一起）。

我们最终没有选"把 reducer 真正改成幂等"（例如把 `drafts`/`drops` 从
`operator.add` 换成按 `draft_id`/`ref` 去重的自定义 reducer，让重复
delta 天然被吸收掉）——那是更彻底但改动面更大的路线，牵涉 fanout 版
更多字段，且需要重新证明"去重 reducer 在正常追加场景下不会误删合法的
重复项"。实际选的是更直接的一条：`existing.values` 非空时，校验参数
一致后**显式 `saver.adelete_thread(thread_id)` 清空整个 thread 再重新
`ainvoke`**——channel 回到"从未跑过"的状态，reducer 不会看到旧值，不
需要证明任何去重逻辑的正确性。代价是这条路径**放弃了 checkpoint 本该
提供的"这次调用是不是从上次断点继续"的增量语义**：thread 跑完后再跑
一次，等价于把上一次的全部 checkpoint 记录连根拔起、从零开始，不是
真正意义上的"幂等重放"——如果调用方指望"用同一 thread_id 重跑只补齐
缺的部分"，这条修复会让它退化成"整个重跑一遍"，只保证了不会产生错误
结果，没有额外节省调用量。这段代码（连同 `run_pipeline_lg` 本身）在
手写版里完全不存在，因为手写版没有"续跑"这个概念，也就没有"续跑时参数
对不上怎么办"以及"跑完之后再跑怎么办"这两个问题需要解决。

**4. `retry_on` 会重试 `ValueError`，而项目刻意的"配置错误当场炸"哨兵
全用 `ValueError`。** 修复前实测：空 `judges` 时 LangGraph 把同一个
确定性错误犯了 3 遍（`review_drafts` 被调用 3 次而不是 1 次，见
`.superpowers/sdd/task-5-report.md` 的 RED 记录）——`RetryPolicy` 默认
不区分"这是一次性配置错误，重试 3 次只会用同样的坏配置再犯 3 次同样的
错"和"这是一次瞬时网络故障，重试可能自愈"。已修复为 `retry_on` 显式排除
`ValueError`（`graph.py:115` 起），修复后同一测试断言调用次数恢复为 1。

**5. msgpack 类型注册：一条被自己推翻的判断（2026-07-27 重做）**

初版这里写的是："checkpoint 反序列化 `Chunk`/`TopicDraft` 走兜底路径，
运行时会打印 `Deserializing unregistered type ... This will be blocked in a
future version`；未来版本收紧后这条路径会直接失效，本项目至今没有注册
`allowed_msgpack_modules`，是真实存在的技术债。"

上量前去还这笔债，把 langgraph 1.2.9 / langgraph-checkpoint 4.1.1 的源码
和行为都实测了一遍，**三处判断都不成立**：

**① 框架已经替你注册了。** `StateGraph.compile(checkpointer=...)` 会调
`_serde.build_serde_allowlist(...)` 递归遍历 state schema 的类型标注，收齐
其中的 pydantic 模型 / dataclass / Enum，再用 `checkpointer.with_allowlist()`
返回一个带 allowlist 的**克隆**。本项目 11 个自定义类型一个不落。
**手写一份 allowlist 反而更差**——它会和 state schema 漂移，框架的推导不会。

**② 未注册的后果不是"失效"，是静默把 pydantic 模型降级成 dict。**
`jsonplus.py` 的 ext hook 在拒绝时走的是 `return tup[2]`——返回该模型的
kwargs dict，不抛异常、不返回 None。症状会表现为下游某个 Node 上一句
`AttributeError: 'dict' object has no attribute 'draft_id'`，**看起来完全
像业务代码的 bug**，排查方向从一开始就是错的。安全加固的失败模式通常是
"拒绝服务"，这里却是"静默换类型"——这个反直觉点值得单独记住。

**③ 真正的坑在开关的组合上，而且比原来那条判断隐蔽得多。**
框架那段自动推导整个被 `if _serde.STRICT_MSGPACK_ENABLED:` 包着
（`graph/state.py:1221`），而这个常量是 import 时从环境变量
`LANGGRAPH_STRICT_MSGPACK` 读死的。也就是说**"序列化器严不严格"和
"要不要自动推导 allowlist"是两个独立开关**，组合起来有一格是陷阱：

| serde 基线 | 环境变量 | 结果 |
|---|---|---|
| 宽松（框架默认） | 未设 | 能跑，但 `with_allowlist()` 是 no-op，allowlist 从未生效，升级即翻车 |
| 严格 | 已设 | 能跑，推导生效（这是框架设计的用法） |
| **严格** | **未设** | **严格拦截照常生效、推导整个跳过 → 全部类型降级成 dict** |

最后这一格正是我们第一版修复踩中的：以为"把 serde 换成严格，compile 会
自动补 allowlist"，结果续跑当场炸出 `AttributeError`——**比什么都不做更糟**。
（RED 记录见 `tests/pipeline/test_checkpoint_serde.py::
test_forgetting_apply_state_allowlist_breaks_resume`。）

**最终修法**：不依赖环境变量，自己调框架那份推导函数并显式
`with_allowlist`（`graph.py::apply_state_allowlist`），配一个空 allowlist
基线的严格 serde（`build_checkpoint_serde`）。代价是引入了本项目唯一一处
langgraph 私有 API 依赖（`langgraph._internal._serde`），已用一条专门的
测试当绊线，升级时会先红。

**这条对"框架强加了什么"的意义**：它强加的不是"你得手写一份类型清单"，
而是**"你得搞清楚两个独立开关的四种组合里哪些是陷阱"**——而这件事在
任何文档里都没写，只能靠读源码 + 实测撞出来。这类隐性认知成本，比代码量
那 1.65× 更难在选型时被算进去。完整论证见
`tests/pipeline/test_checkpoint_serde.py` 的模块文档。

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

**这次全分支审查后的补充核实**：Critical 1（commit `7c4ddd2`）给
`run_pipeline_lg` 加了一个新分支（`existing.values` 非空时的
一致性校验 + `adelete_thread`），`graph.py` 净增约 30 行，但这 30 行
里绝大多数是解释修复动机的注释块，按同一把尺子（非注释正文行）粗算，
真正落进"断点续跑相关代码"这一类目的新增行数只有个位数——`139`
（编排机制，未涉及 `run_pipeline_lg`）不受影响，`192` 这个总数、
`2.3×` 这个比值在四舍五入的精度上也没有变化。这里没有重新做一次
Task 6/8 那样逐行精确核对，只是抽样确认了"改动完全落在断点续跑分支
内、比值量级未变"这个结论，如果要精确到个位数应重新逐行数一遍。

### 一条正面素材

LangGraph 只把最终成功那次调用的返回值当作该 step 的 delta，失败的
重试尝试不产生 delta——所以 reducer 累加语义在重试下不会翻倍，这件事
框架帮你兜住了。**这条是我对 LangGraph Pregel 执行模型的判断**（重试
是重新调用整个 Node 函数，只有不抛异常那次的返回值会被记为该 step 的
delta），本项目没有专门写测试去实测验证这个机制本身，不是"实测表明"。

## 第二章：额外解锁（B 阶段：`Send` 扇出到条目级）

> **声明：B 阶段已不是"同一需求的两种实现"——架构变了。** 它回答的是
> 另一个问题："框架让我做成了原本不会去写的事吗，收益多大？"
> `graph_fanout.py` 没有接入 `test_run.py` 那 14 条两引擎对等的参数化
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
239 个用例里持续通过，不是一次性观察。

### 真并发：这次实验唯一低估框架的地方

前面两节量的都是"checkpoint resume 净成本"，但 B 阶段还有一个完全
不同维度的收益：`Send` 扇出让 `extract_one`/`review_one` 这两层从
"整批一次调用、内部顺序处理"变成"逐条目真并发调用"，这件事在本文
用到的 fake 实验装置里被系统性地抹平了——`_fake_deps()` 的 extractor
是瞬时返回的纯函数，不管并发度是 1 还是 100，墙钟耗时都趋近于 0，
两者的差异在 `compare_orchestration.py` 的耗时列上完全不可见。

以下是**一次性探针实测，未固化为测试**（代码库里搜不到这组数字，只
在 `graph_fanout.py` 的 `DEFAULT_MAX_CONCURRENT_LLM_CALLS` docstring
里被引用过，读者需要自行复现才能验证）：8 个 chunk，extractor 换成
`sleep(1s)` 的假实现——

```
langgraph（A 阶段，Node 粒度，无扇出）  N=8 → 墙钟 8.08s，extractor 并发峰值 = 1
fanout（B 阶段，条目级扇出）           N=8 → 墙钟 1.05s，extractor 并发峰值 = 8
```

约 **8×** 墙钟加速，加速比与并发度基本对齐（8 个 chunk、并发峰值 8）。
这是框架相对手写版最值钱的一处收益，本文其余章节的实验装置从未测出
过它。

但反面同样要写清楚：并发无上界时，真实课标几百个条目会瞬间打出几十路
并发 LLM 请求，直接撞 provider 的速率限制——而 `extract_all`/
`review_drafts`/`review_edges` 六层纯函数一律"逐条 `try/except`：非
编程错误转成 `DropRecord`、`continue`"（同上文"框架强加了什么"第 1
条），意味着并发失控的后果**不是崩溃、是安静地把大半 draft/review
结果吞成 `DropRecord` 丢掉**——`RetryPolicy` 按本文自己的结论根本
够不着这类故障（它只在整层调用彻底失败时触发）。全分支审查发现这个洞
后（Important 1，commit `c2db4e6`），`build_fanout_graph` 现在默认
`max_concurrency=8`，用 `asyncio.Semaphore` 包住 `extract_one`/
`review_one`。**这个 8 是工程判断，不是校准过的数字**——docstring 里
明确写了"未做任何 provider 端实测校准"，生产接入前应按实际 provider
（本项目目前是 DeepSeek）的并发/QPS 配额重新核实。

### 这个收益要用什么代价换

第一章"框架强加了什么"的全部 7 条（Node 级重试不可达、`NODE_TIMEOUT`
不提供墙钟上界、幂等化要求、`retry_on`/`ValueError`、msgpack 开关组合的陷阱、
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
（或 2.3×，如果还要用断点续跑）的代码量、msgpack 严格模式的开关陷阱、异步传染，
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
流水线（本项目 159→239 个测试的路子），引入 LangGraph 的决策不该基于
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
