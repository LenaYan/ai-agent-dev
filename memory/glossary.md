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
