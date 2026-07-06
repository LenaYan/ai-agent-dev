# 决策记录 (decisions) — ADR 风格

记录学习与实践中的关键选择。**每条必须写"为什么"和"取舍"。**
推翻旧决策时：新增一条，在 `影响` 里引用旧编号（如 "取代 ADR-0002"），旧条目标注 `[已被 ADR-000X 取代]`。

格式：
```
## ADR-000N — <标题>  (YYYY-MM-DD)
- 背景：<面临什么问题>
- 决定：<选了什么>
- 理由：<为什么，核心 1-2 点>
- 取舍/放弃项：<放弃了什么，代价是什么>
- 影响：<后续约束 / 引用关系>
```

---

## ADR-0001 — 学习主力语言选 Python  (2026-07-06)
- 背景：AI Agent 生态需要一门主力语言做 sample 与练习。
- 决定：Python 为主，必要时补 TypeScript/Node。
- 理由：Agent 框架（LangGraph、LlamaIndex、AutoGen、CrewAI、OpenAI/Anthropic SDK）Python 生态最完整、示例最多。
- 取舍/放弃项：放弃 TS-first 带来的类型安全与前端一体化；需要时单独用 TS 做前端/边缘场景。
- 影响：samples/practice 默认 Python + venv/uv。
