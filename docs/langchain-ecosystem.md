# LangChain 生态辨析：LangChain / LangGraph / LangSmith / Langfuse

> 编写日期：2026-07-06 · ⚠️ 代码为示意，API 迭代快，以官方最新文档为准。

## 心智模型（先记这个）

这四个分属**两个维度**，不是互相替代：

```
构建层（写代码）：  LangChain（组件）  →  LangGraph（编排 Agent）
观测层（跑起来后）： LangSmith（闭源）   或   Langfuse（开源·第三方）
```

- **前三个**是同一家公司 **LangChain Inc.** 的产品；**Langfuse 是独立第三方开源项目**。
- LangChain / LangGraph 是**库**（你写代码时用）；LangSmith / Langfuse 是**平台**（代码跑起来后观测/评估）。

| 名字 | 类别 | 一句话 | 出品方 |
|---|---|---|---|
| LangChain | 组件库 | 模型/prompt/工具/RAG 的标准积木 | LangChain Inc. |
| LangGraph | 编排框架 | 用图/状态机编排复杂 Agent | LangChain Inc. |
| LangSmith | 可观测/评估平台 | 闭源 SaaS，LLM 版 APM | LangChain Inc. |
| Langfuse | 可观测/评估平台 | 开源、可自托管、框架中立 | Langfuse（无关） |

---

## 1. LangChain — 组件库

**解决**：把「调模型、拼 prompt、接工具、RAG、串多步」封装成可复用组件。
**机制**：Model / Prompt / Tool / Retriever / Chain 抽象 + LCEL（`|` 管道）串联。
**现状**：抽象偏厚、版本波动被诟病；官方已把**复杂 agent 编排推向 LangGraph**，LangChain 更多作底层组件层。

```python
# LCEL：prompt | model | 输出解析，串成一个 chain
from langchain_core.prompts import ChatPromptTemplate
from langchain_openai import ChatOpenAI
from langchain_core.output_parsers import StrOutputParser

prompt = ChatPromptTemplate.from_template("用一句话解释 {topic}")
chain = prompt | ChatOpenAI(model="gpt-4o-mini") | StrOutputParser()
print(chain.invoke({"topic": "ReAct 模式"}))
```

---

## 2. LangGraph — Agent 编排框架

**解决**：线性 chain 表达不了真实 Agent 的**循环、条件分支、多 agent、人在环、可中断/恢复**。
**机制**：定义 **State（状态）+ Node（节点）+ Edge（边，含条件边）**，Agent = 在图上流转的状态机。
**地位**：当前 LangChain 生态做**生产级 Agent** 的主推，建议重点学。可用 LangChain 组件，但不强依赖。

```python
# 最小状态机：一个节点 + 条件边决定是否循环
from typing import TypedDict, Annotated
from langgraph.graph import StateGraph, START, END
import operator

class State(TypedDict):
    count: Annotated[int, operator.add]  # reducer：节点返回值累加

def step(state: State):
    return {"count": 1}

def should_continue(state: State):
    return "loop" if state["count"] < 3 else "done"

g = StateGraph(State)
g.add_node("step", step)
g.add_edge(START, "step")
g.add_conditional_edges("step", should_continue, {"loop": "step", "done": END})
app = g.compile()
print(app.invoke({"count": 0}))  # -> {'count': 3}
```

> 关键概念：**reducer**（如 `Annotated[list, operator.add]`）决定节点返回值是「覆盖」还是「累加」旧状态——初学最常踩的坑（见 pitfalls）。

---

## 3. LangSmith — 闭源可观测/评估平台

**解决**：Agent 是非确定性黑盒——「走了哪几步、每步 prompt/输出/token/耗时、为什么错」。
**机制**：埋点上报每次运行的完整 trace，可视化调试 + 跑评估集 + 线上监控。
**特点**：框架无关（也能观测非 LangChain 代码），但对 LangChain/LangGraph **零配置自动接入**。闭源 SaaS，有免费额度。

```bash
# 通常只要设环境变量，LangChain/LangGraph 运行即自动上报
export LANGSMITH_TRACING=true
export LANGSMITH_API_KEY=ls-...
```

---

## 4. Langfuse — 开源可观测/评估平台

**解决**：与 LangSmith **同一件事**，但**开源、可自托管、框架中立**（对 OpenAI SDK、LlamaIndex 等一视同仁）。
**选择动因**：数据不出内网 / 自托管 / 不想被 LangChain 生态绑定 / 省钱 → Langfuse；深度用 LangChain 全家桶、要最丝滑集成 → LangSmith。

```python
# Langfuse：通过回调/装饰器接入，可用于任意框架
from langfuse.decorators import observe

@observe()
def my_agent_step(q: str) -> str:
    ...  # 你的逻辑，自动被 trace
```

---

## 常见误区

- ❌「LangGraph 取代 LangChain」→ 分工：LangGraph 编排，LangChain 供组件。
- ❌「LangSmith / Langfuse 是框架」→ 不是，是观测/评估平台，不写业务逻辑。
- ❌「Langfuse 是 LangChain 出的」→ 独立第三方开源项目。
- ❌ 一上来学 LangChain 全家桶 → 资深路径：**先手写原理 → 直接学 LangGraph → 有 Agent 能跑了再接观测层（LangSmith 或 Langfuse 二选一）**。

## 学习建议

1. 手写「LLM + 工具调用」最小循环（不用框架），理解框架帮你隐藏了什么。
2. 学 LangGraph 的 State/Node/Edge/reducer，重写上面的 Agent。
3. 接一个观测平台，看真实 trace，建一个小 eval 集。
