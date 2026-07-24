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

## 2026-07-06 — 支持 Copilot CLI + Claude Code 双工具
- 收获：Claude Code 默认只读 CLAUDE.md（不读 AGENTS.md），Copilot 两者都读。以 AGENTS.md 为单一事实来源，CLAUDE.md 用 @AGENTS.md 导入桥接，避免重复维护。工具专属配置分放 .github/ 与 .claude/。
- 产出：CLAUDE.md、AGENTS.md §6、.gitignore、ADR-0003
- 下一步：手写「模型在循环里用工具」最小骨架。

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

## 2026-07-24 — 课标知识依赖图：schema + CI 校验层（TDD）
- 收获：①"规则能查的"和"查不了的"要分层设计——结构层（环/悬挂/单调性）纯规则即可，Marble 这层做得扎实（0环0悬挂0孤立）；真出问题的是内容层（name与description讲的不是一回事），只能靠语义判断，故把 judge 做成依赖注入（CI 接真 LLM，测试接确定性假判定器）。②"跳过"必须留痕：不传 judge 时产出 CONSISTENCY_SKIPPED 警告而非静默通过，否则"CI绿了"会被读成"全查过了"。③跑真实数据是最好的测试：单测全绿后跑 Marble 1590节点/3221边，当场暴露两个自己的 bug（报告按 code 分组导致 error 被藏进 warning 组；适配器漏映射 standards 致对齐率虚报为 0%），两个都先补失败测试再修。
- 产出：practice/cn-curriculum-graph/（schema + 9 条校验规则 + CLI + Marble 适配器 + 38 测试）
- 下一步：生成流水线（多 agent 抽取课标 → 交叉审核 → 产出「数与代数」首批节点）。法律定性未决前不投入大规模生成。
