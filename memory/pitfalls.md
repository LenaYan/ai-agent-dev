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
