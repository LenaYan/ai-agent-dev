# 学习日志 (learning-log)

按时间倒序记录学习进度。每条：日期 · 主题 · 关键收获 · 产出物（sample/notes 链接）。

格式：
```
## YYYY-MM-DD — <主题>
- 收获：<1-3 条核心要点>
- 产出：<samples/xxx 或 docs/xxx 或 无>
- 下一步：<可选>
```

---

## 2026-07-06 — 构建有效 Agent 三原则（Anthropic）
- 收获：一手权威源（Barry Zhang & Erik Schluntz）。①不要什么都做成 Agent（workflow vs agent，用复杂度/价值/可行性/错误成本 4 条清单判断；写代码是好场景因为可用单测/CI验证）②尽可能久地保持简单（Agent=模型在循环里用工具，骨架=环境+工具+SystemPrompt，功夫在工具设计+prompt）③像 Agent 一样思考（=上下文工程，用模型 debug 模型）。核心：有廉价验证回路的任务才适合高自主 Agent。已升格为工作区默认心法。
- 产出：docs/effective-agents-principles.md，并在 AGENTS.md/roadmap.md 引用
- 下一步：手写「模型在循环里用工具」最小骨架。

## 2026-07-06 — Agent 架构模式目录
- 收获：架构分三正交维度（A单Agent推理范式/B多Agent拓扑/C编排基础设施）+ 跨维度Router+Skill，不是1→7进化线。核心原则「简单优先」，多Agent默认不用。纠正社区分享的硬伤：ReAct=Reason+Act(非Actor)、LangGraph支持环非DAG、LangGraph≠Blackboard、Airflow/Prefect非agent框架、Router+Skill非AI-coding唯一最佳实践。
- 产出：docs/agent-architecture-patterns.md（对一份社区分享的修正重构）
- 下一步：手写「LLM+工具调用」最小循环。

## 2026-07-06 — LangChain 生态辨析
- 收获：LangChain(组件库)/LangGraph(图状态机编排,生产主推)/LangSmith(闭源观测评估)/Langfuse(开源第三方观测)。前三同公司,Langfuse无关。构建层 vs 观测层两个维度,非替代关系。LangGraph 的 State/Node/Edge/reducer 是核心。
- 产出：docs/langchain-ecosystem.md、glossary 新增 5 条术语
- 下一步：手写「LLM+工具调用」最小循环。

## 2026-07-06 — 职业定位与市场/技术背景分析
- 收获：Agentic AI 岗位近年增长 280%~380%，需求超供给；护城河在工程化+场景落地+架构，"只会调 API"贬值。中国以大厂/传统行业转型为主，全球以金融/医疗/企业+AI 实验室为主，全球薪资绝对值更高。技术上行业回归"单 Agent 简单优先"，多 Agent 仅特定场景；框架首选 LangGraph，关注 MCP、可观测、安全。
- 定位：作为 20 年老兵，主打"工程化+领域+架构"组合，绑定一个垂直领域做端到端项目，而非拼"新"。
- 产出：docs/career-and-market-analysis.md
- 下一步：按 roadmap 阶段一，手写"LLM + 工具调用"最小循环。

## 2026-07-06 — 工作区初始化
- 收获：搭建 AI Agent 学习工作区的基础配置与记忆系统。
- 产出：AGENTS.md、.github/copilot-instructions.md、docs/roadmap.md、memory/*。
- 下一步：从 `docs/roadmap.md` 阶段一开始。
