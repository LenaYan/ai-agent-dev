# 踩坑笔记 (pitfalls)

记录踩过的坑和解法，避免重复。每条：现象 · 原因 · 解法 · 日期。

格式：
```
## <一句话现象>  (YYYY-MM-DD)
- 原因：<根因>
- 解法：<怎么解决 / 规避>
- 备注：<可选，如版本相关>
```

---

## LLM 判定器"淘汰率异常高"时，先看分歧率再改 prompt  (2026-07-27)
- **现象**：二值 fidelity 判定淘汰了 62% 的产出，图稀疏到没法用。第一反应是"prompt 写得不好"或"标准太严"。
- **真相**：45 条淘汰里 **21 条（47%）是分歧淘汰**——两个模型看法相反，且**方向随机**（同类样本上 A 判过 B 判否，换个样本反过来）。这不是标准松紧问题，是**判定档位不够**：模型面对"没有合适选项"时不会告诉你，它会硬塞进现有某一档，塞哪一档是随机的。
- **解法**：加档，不是调 prompt。二值 → 三档后误杀 45→0。
- **诊断动作（可直接复用）**：逐票统计而不是逐结论统计。`Counter(tuple(v['approved'] for v in outcome['votes']))` 一行就能把"都否 / 分歧"分开。**分歧率高 = 档位不够；一致否决率高 = 标准或 prompt 问题。** 两者的修法完全不同。
- **陷阱**：读 outcome 时别只打印 `votes[0]` —— 全票通过才算过，`votes[0]` 完全可能是赞成票，会让你误以为"模型的理由和它的投票自相矛盾"。我踩过一次，差点据此做了错误的重构。
- **反直觉的一条**：为压掉漏报加针对性规则，**实测让漏报变多**（2→3）——规则把模型注意力全引向"多写"，挤掉了"少写（内容缺失）"。判定质量不是加规则就单调上升的；每加一条都要跑 ground truth，别凭感觉。

## 判定器的闸门要按代价不对称定，不是按准确率  (2026-07-27)
- **现象**：三档 fidelity 评测 17/20 = 85%，"误杀 0、漏报 2"。按"准确率 ≥90% 才算过"这种闸门会判失败，然后就会去调 prompt 追那 15%。
- **问题**：85% 这个数字把两类方向相反的错误混成了一个标量。实际代价差一个数量级——**误杀**（合格被淘汰）让图稀疏到没法用，已实测；**漏报**（编造被放行）只是让个别节点混进一张 `review_status=unreviewed` / `confidence=0.0` 的图，而那正是 provenance 诚实性设计要兜的场景。
- **解法**：闸门按方向分级，不看总准确率。误杀非零即红；漏报**记显式基线**（当前 2）、变差才红。基线写进代码注释并说明每条为什么压不掉（其中一条根因在上游抽取层，不在判定器）。
- **要点**：基线是**已知限制的显式记录，不是目标**。把它调低要靠证据，不是靠调低期望。

## "需要全局视野"不等于"必须串行"  (2026-07-27)
- **现象**：`review_edges` 一直是串行的，理由是"它需要全局 kept_ids"。真实语料上量后变成 690 次串行 LLM 调用，撞上 10 分钟 node timeout，总耗时 49:32。
- **原因**：把两件事混为一谈了。它需要全局视野才能**开始**（要先知道哪些 draft 活着），但开始之后**每条边的判定彼此独立**。"需要全局视野"阻止的是**扇出**（把任务 Send 到条目级、由编排层调度），**不是层内并发**。
- **解法**：函数内部开线程池并发，对外仍是同步函数。49:32 超时 → 2:27。
- **推论**：**"并发上限校准好了"不等于"整条流水线都并发了"**。校准只覆盖了编排层的两个扇出点，`review_edges` 和 `dedupe` 从来没被并发化——只是以前它们没活干，所以没人发现。上游一改（判定放宽、存活率上升），瓶颈立刻转移到没人看的那一层。
- **并发化一个既有纯函数必须守住的三条**：①**产物顺序须与串行完全一致**（若有逐字节比对产物的对等性测试，完成顺序泄漏进排列会让它变成随机红绿，比慢糟糕得多）；②**异常须在工作线程就地转成返回值**（`executor.map` 是惰性的，一旦某任务抛出，它之后的结果就取不到，"单条失败不中断整批"会静默失效）；③**程序错误须显式还原类型再 raise**（线程池会包装异常，`except PROGRAMMING_ERRORS` 那道防线在并发路径上否则失效）。

## 教育部课标 PDF 是扫描件，pdftotext 提不出字  (2026-07-27)
- **现象**：`http://www.moe.gov.cn/srcsite/A26/s8001/202204/W020220420582346895190.pdf`（义务教育数学课程标准 2022 年版，40MB / 189 页）下载正常，`pdftotext -layout` 输出 0 行。
- **原因**：扫描件。`pdfinfo` 显示 `Producer: Adobe Acrobat 18.11 Image Conversion Plug-in`，`pdffonts` 输出空 —— 没有文字层。
- **解法**：OCR，或用支持视觉读 PDF 的工具逐页转录。小学·数与代数【内容要求】在正文 p.17–26（三个学段共 28 条），PDF 页码 ≈ 正文页码 + 7。
- **顺带**：`pdffonts` 输出为空是判断"扫描件 vs 文字版"最快的一招，比试着提取再看结果快。

## LangGraph：只把 serde 改严格，反而比不改更糟  (2026-07-27)
- **现象**：为了提前适配"未来版本会强制拦截未注册 msgpack 类型"，把 checkpointer 的 serde 换成严格模式（`JsonPlusSerializer(allowed_msgpack_modules=())`）。改完 happy path 全绿，但**崩溃后 checkpoint 续跑当场炸** `AttributeError: 'dict' object has no attribute 'draft_id'`。
- **原因**：langgraph（1.2.9）里这是**两个独立开关**——(a) 序列化器严不严格，由你传的 `allowed_msgpack_modules` 决定；(b) 要不要**自动从 state schema 推导 allowlist**，由 `StateGraph.compile()` 里的 `if _serde.STRICT_MSGPACK_ENABLED:` 决定，而这个常量是 import 时从环境变量 `LANGGRAPH_STRICT_MSGPACK` 读死的。四种组合里 `严格 serde + 未设环境变量` 是陷阱格：严格拦截照常生效，推导整个跳过 → 所有自定义类型的 allowlist 为空。
- **解法**：不依赖环境变量，自己调 `langgraph._internal._serde.build_serde_allowlist(schemas=[...], channels=...)` 再 `saver.with_allowlist(...)`。（用了私有 API，配一条测试当绊线，升级时先红。）
- **额外要记住的**：未注册类型的失败模式**不是拒绝，是静默降级成 dict**（ext hook 拒绝时 `return tup[2]` 返回该模型的 kwargs）。所以症状永远出现在**下游业务代码**上，看起来像业务 bug——**续跑路径上遇到莫名其妙的 `'dict' object has no attribute ...`，先查序列化配置，别去 debug 业务逻辑。**
- 备注：langgraph 1.2.9 / langgraph-checkpoint 4.1.1。另：`compile(checkpointer=saver)` 返回的是带 allowlist 的**克隆**，直接拿原 saver `aget_tuple()` 会看到降级后的 dict——这个不一致是设计如此，曾把我们误导过一轮。

## `asyncio.to_thread` 有一道隐藏并发天花板  (2026-07-27)
- **现象**：把并发上限从 8 调到 32，实测在飞峰值只有 20，吞吐不再增长、p95 反而翻倍。
- **原因**：`asyncio.to_thread` 用的是事件循环的**默认 executor**，CPython 的 `ThreadPoolExecutor` 默认 `max_workers = min(32, os.cpu_count() + 4)`。信号量放行再多，多出来的只会排队。
- **解法**：并发上限默认值直接取 `min(32, cpu_count+4)`（写成公式，别抄某台机器上的具体数字）；要真正提高必须先 `loop.set_default_executor()` 换更大的池。设置超过这道墙时**发警告而不是静默截断**——否则现象是"我明明调到 64 了怎么没变快"，代码里没人告诉你原因。
- **值得记住的因果**：这道墙常常是**自己引入的**。本项目是为了让 LangGraph 的 per-node `timeout` 生效，必须把阻塞的 SDK 调用改成 `await asyncio.to_thread(...)`——超时能力和并发天花板是同一个决定的两面。
- 备注：另一半认知修正是 provider 侧——DeepSeek 不设 RPM/TPM，只设**账户级并发连接数**（v4-flash 2500 / v4-pro 500，超出返 429），所以"为了不撞限流而调低并发"这个直觉在它身上基本用不上。

## 概念辨析纠偏：社区资料常见的 5 处错误  (2026-07-06)
来源：一份社区「7 种 Agent 架构」分享，与 Anthropic 一手资料/官方文档核对后纠正。
- **ReAct 全称**：是 Reason + **Act**（Reasoning and Acting），不是「Reason + Actor」。
- **LangGraph ≠ DAG**：它支持**环（cycle）**可循环回退，正是区别于 Airflow/Prefect 纯 DAG 引擎之处；说它「基于有向无环图」是错的。
- **LangGraph ≠ Blackboard**：LangGraph 是图/状态机编排；Blackboard 是「共享知识库 + 机会式读写」的经典范式，二者机制不同。
- **Airflow / Prefect / n8n 不是 agent 框架**：它们是通用数据/任务流水线或低代码自动化，别和 LangGraph 混为一类。
- **Router+Skill 不是 AI Coding「唯一最佳实践」**：主流 AI coding agent（Cursor/Claude Code/Copilot）更多是单 Agent + 丰富工具 + 强上下文工程；Router+Skill 只是可靠性收益高的一种模式。
- 备注：判断架构别按「1→7 进化线」，按三正交维度（推理范式/协作拓扑/编排基础设施）选。详见 docs/agent-architecture-patterns.md。

<!-- 示例：
## LangGraph 状态在节点间没有累加  (YYYY-MM-DD)
- 原因：state schema 用了普通赋值而非 reducer，节点返回覆盖了旧值。
- 解法：对需累加的字段用 Annotated[list, add] 之类的 reducer。
- 备注：LangGraph 0.x，API 可能变化。
-->

## 2026-07-24 — 按单一 key 分组做报告会把高严重级藏进低严重级
- 坑：格式化校验报告时按 `code` 分组，用 `group[0].severity` 决定整组的显示标记。同一 code 严重级可能不同（GRADE_INVERSION 在 hard 边是 ERROR、soft 边是 WARNING），结果 7 条 error 被显示成 warning 组，CI 看起来像只有警告。
- 解法：按 `(code, severity)` 复合键分组。
- 教训：分组键必须覆盖所有影响展示的维度。单测没抓到（测试数据同 code 同严重级），是跑真实数据才暴露的。
