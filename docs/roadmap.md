# AI Agent 开发学习路线图

> 面向有 ~20 年经验的工程师：跳过编程基础，聚焦 **Agent 特有的原理、工程与生产实践**。
> 建议每阶段：读原理 → 写最小 sample → 记 memory。领域迭代快，具体框架 API 请以官方最新文档为准（本图侧重不易过时的概念骨架）。

---

## 阶段一 · LLM 与 Agent 的地基（1-2 周）
理解 agent 之所以能"自主"的底层能力。

- LLM 推理基础：token、上下文窗口、温度/采样、成本与延迟权衡
- 提示工程进阶：结构化输出、few-shot、思维链 (CoT)、self-consistency
- **Function / Tool Calling**：结构化工具调用协议（agent 与世界交互的核心）
- 结构化输出与校验：JSON mode、`pydantic`、schema 约束
- 🎯 产出：不用任何框架，用原生 SDK 手写一个"LLM + 工具调用"最小循环

## 阶段二 · Agent 核心范式（2-3 周）
从"调一次模型"到"会循环、会规划"。

- **ReAct**：推理-行动-观察循环（几乎所有 agent 的雏形）
- Planning：任务分解、Plan-and-Execute、树/图式规划
- Reflection / Self-critique：让 agent 自查与迭代改进
- 循环控制：终止条件、最大步数、防死循环、错误恢复
- 🎯 产出：手写一个 ReAct agent（不依赖框架），再对照框架实现理解差异

## 阶段三 · 上下文与记忆系统（2 周）
Agent 的"记性"决定它能力上限。

- 上下文工程 (Context Engineering)：如何在有限窗口里塞对信息
- 短期 vs 长期记忆；情节记忆 vs 语义记忆
- **RAG**：切分、embedding、向量库（FAISS/Chroma/pgvector）、检索策略、rerank
- 高级检索：hybrid search、query 改写、GraphRAG 概念
- 记忆持久化：向量库 + 关系库组合
- 🎯 产出：给阶段二的 agent 加上一个可检索的长期记忆

## 阶段四 · 主流框架（3-4 周）
理解了原理后再用框架，才知道它帮你隐藏了什么。

- **LangGraph**：图/状态机式编排（当前生产 agent 的主流选择之一）
- **LlamaIndex**：数据/RAG 为中心
- **OpenAI Agents SDK** / **Anthropic** 工具生态
- **AutoGen** / **CrewAI**：多 agent 编排
- 横向对比：编排模型、状态管理、可控性、生产成熟度
- 🎯 产出：同一个需求分别用"手写"和"某框架"实现，写对比笔记进 docs/

## 阶段五 · 工具、协议与集成（2 周）
让 agent 真正"能干活"。

- 工具设计：粒度、幂等、错误返回、给模型的描述怎么写
- **MCP (Model Context Protocol)**：标准化工具/数据源接入（重点，生态在快速形成）
- 代码执行、浏览器/计算机操作类工具的安全沙箱
- 🎯 产出：写一个自定义工具 + 接一个 MCP server

## 阶段六 · 多 Agent 系统（2 周）
- 编排模式：supervisor、hierarchical、swarm、handoff
- Agent 间通信与共享状态
- 何时"多 agent"是过度设计（多数场景单 agent + 好工具更优）
- 🎯 产出：一个 supervisor + 子 agent 的小系统

## 阶段七 · 评估、可观测与质量（2-3 周）
把"能跑的 demo"变成"可信的系统"。

- 评估：任务成功率、轨迹评估、LLM-as-judge、离线 eval 集
- 可观测性：tracing（LangSmith / Langfuse / OpenTelemetry）、token/成本监控
- 测试 agent：非确定性下如何写可靠测试
- 🎯 产出：给某个练习 agent 接 tracing + 建一个小 eval 集

## 阶段八 · 生产化（2-3 周）
- 可靠性：重试、超时、幂等、降级、人在环 (human-in-the-loop)
- 安全：prompt injection、工具权限最小化、输出校验、越权防护
- 成本与延迟优化：缓存、模型分级路由、并行
- 部署：流式响应、有状态会话、异步/后台任务
- 🎯 产出：把某个练习 agent 打磨到"敢给别人用"的程度

---

## 贯穿始终的习惯
- 每个主题都动手写最小可运行 sample，别只看不练。
- 学完更新 `memory/`（日志/决策/术语/坑）。
- 关注 prompt injection 与密钥安全，从第一天养成。
- 框架 API 变化快：以官方文档为准，本图只给概念骨架。

## 推荐信息源（自行核实时效）
- 各框架官方文档（LangGraph、LlamaIndex、AutoGen、CrewAI 等）
- Anthropic / OpenAI 官方 cookbook 与 agent 指南
- MCP 官方规范与示例
