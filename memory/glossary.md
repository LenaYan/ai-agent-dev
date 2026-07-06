# 术语表 (glossary)

Agent 领域关键术语，按需追加。每条：术语 · 一句话定义 · 关键点/易混淆。

格式：`**术语 (英文)** — 定义。要点/对比。`

---

- **Agent** — 由 LLM 驱动、能自主决定"下一步做什么"并调用工具与环境交互的系统。区别于纯问答：有循环、有状态、有工具。
- **ReAct** — Reason + Act 交替的提示范式：模型先推理再行动（调工具），观察结果后继续。多数早期 agent 循环的基础。
- **Tool / Function Calling** — 让模型以结构化方式请求调用外部函数/API 的能力；agent 与外界交互的主要手段。
- **RAG** — Retrieval-Augmented Generation：检索外部知识注入上下文再生成，缓解幻觉与知识过时。
- **MCP (Model Context Protocol)** — Anthropic 提出的开放协议，标准化 agent/LLM 与外部工具、数据源的连接（可类比"AI 的 USB-C"）。
- **Memory** — agent 跨轮/跨会话保留信息的机制：短期（上下文窗口）、长期（向量库/数据库）、情节/语义记忆等。
- **LangChain** — 构建 LLM 应用的基础组件库（Model/Prompt/Tool/Retriever/Chain）。现主要作底层组件层，复杂 agent 编排已被官方推向 LangGraph。同公司出品。
- **LangGraph** — 用「State + Node + Edge（含条件边）」把 Agent 建成图/状态机的编排框架，支持循环、分支、多 agent、人在环、可中断恢复。当前 LangChain 生态做生产级 agent 的主推。同公司出品。
- **LangSmith** — LangChain 公司的闭源可观测/评估 SaaS：tracing、调试、eval、监控（LLM 版 APM）。框架无关，但对 LangChain/LangGraph 零配置接入。
- **Langfuse** — 与 LangSmith 同类，但开源、可自托管、框架中立的第三方（与 LangChain 无关）。自托管/数据不出内网/去绑定 → 选它。
- **LCEL** — LangChain Expression Language，用 `|` 管道语法把组件串成 chain。
