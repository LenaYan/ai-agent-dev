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
