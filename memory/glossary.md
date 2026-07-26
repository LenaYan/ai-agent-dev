# 术语表 (glossary)

Agent 领域关键术语，按需追加。每条：术语 · 一句话定义 · 关键点/易混淆。

格式：`**术语 (英文)** — 定义。要点/对比。`

---

- **Agent** — 由 LLM 驱动、能自主决定"下一步做什么"并调用工具与环境交互的系统。区别于纯问答：有循环、有状态、有工具。
- **ReAct** — Reason + Act（Reasoning and Acting）交替的提示范式：模型先推理再行动（调工具），观察结果后继续。多数早期 agent 循环的基础。⚠️ 是 Reason + **Act**，不是「Reason + Actor」。
- **Tool / Function Calling** — 让模型以结构化方式请求调用外部函数/API 的能力；agent 与外界交互的主要手段。
- **RAG** — Retrieval-Augmented Generation：检索外部知识注入上下文再生成，缓解幻觉与知识过时。
- **MCP (Model Context Protocol)** — Anthropic 提出的开放协议，标准化 agent/LLM 与外部工具、数据源的连接（可类比"AI 的 USB-C"）。
- **Memory** — agent 跨轮/跨会话保留信息的机制：短期（上下文窗口）、长期（向量库/数据库）、情节/语义记忆等。
- **LangChain** — 构建 LLM 应用的基础组件库（Model/Prompt/Tool/Retriever/Chain）。现主要作底层组件层，复杂 agent 编排已被官方推向 LangGraph。同公司出品。
- **LangGraph** — 用「State + Node + Edge（含条件边）」把 Agent 建成图/状态机的编排框架，支持循环、分支、多 agent、人在环、可中断恢复。当前 LangChain 生态做生产级 agent 的主推。同公司出品。
- **LangSmith** — LangChain 公司的闭源可观测/评估 SaaS：tracing、调试、eval、监控（LLM 版 APM）。框架无关，但对 LangChain/LangGraph 零配置接入。
- **Langfuse** — 与 LangSmith 同类，但开源、可自托管、框架中立的第三方（与 LangChain 无关）。自托管/数据不出内网/去绑定 → 选它。
- **LCEL** — LangChain Expression Language，用 `|` 管道语法把组件串成 chain。
- **Workflow vs Agent** — Workflow：预定义控制流里编排多次模型调用（决策树清晰时用）；Agent：模型自主决定行动路径并按环境反馈行动（问题空间模糊、高价值、可验证时才用）。核心判断见 effective-agents-principles.md。
- **Blackboard（黑板系统）** — 经典 AI 协作范式：多 agent 共享一块知识库/状态，机会式读写，状态变化驱动行动。≠ LangGraph（后者是图/状态机编排）。
- **DAG vs 图（含环）** — Airflow/Prefect 等是 DAG（有向无环，不能循环）；LangGraph 的卖点恰是**支持环（cycle）**，能循环回退，故不是 DAG。
- **知识依赖图 / 先修图 (prerequisite graph / skill taxonomy)** — 把领域知识拆成细粒度节点、用「先修」有向边连接的结构化图，供 LLM 在其上做定位与调度。与传统知识图谱的区别：边带强弱与**人可读理由**、节点带掌握判据，是**为 LLM 消费**而非规则引擎设计的。代表：Marble os-taxonomy（2026-07 开源，1590 节点 / 3221 边）。
- **hard vs soft 依赖** — 先修边的强弱标注：hard = 不掌握就学不动，soft = 有助于理解但非必需。工程含义：校验规则（如年级倒挂）不能一刀切，hard 边判 error、soft 边判 warning，否则必然过噪或漏报。
- **evidence criteria / assessment prompt** — 知识节点上的「怎么算掌握了」与「用什么问题去验」。把教研团队才产出的评测设计模板化成 LLM 可执行指令，是这类数据集商业价值最高的部分。
- **provenance（数据来源元数据）** — 每条数据的生产方法 / 审核状态 / 审核人 / 置信度。没有 provenance 和公开 QA 流程的知识图谱是红旗：一条错边静默导偏整条学习路径，使用者发现不了。
- **BKT / DKT / IRT** — 学习者状态建模一系（Bayesian Knowledge Tracing / Deep KT / Item Response Theory），建模掌握度、遗忘与部分掌握。依赖图只建模「顺序」，这层建模「学到什么程度」，需大量真实作答数据标定 —— 也正是开源数据集普遍缺的那一层。
- **ODbL 1.0 / CC BY-SA 4.0** — 开源数据集常见组合（数据库用 ODbL、内容用 CC BY-SA）。要点：商用**不要求**开源自己的产品代码，只要求把对该数据集本身的改进回馈；copyleft 不传染到应用层。
- **LLM-as-judge（LLM 判定器）** — 用一个 LLM 调用做有界判断（一致性/相关性/优劣打分），产出结构化结论供流水线消费。工程要点：做成依赖注入的 Protocol（CI 接真 LLM、测试接确定性假判定器）；temperature=0 求可复现；先便宜模型跑 ground truth 量准确率，不够再升级。多 judge 投票 = 交叉审核层的基本形态。
- **结构化输出 / messages.parse（Anthropic SDK）** — `client.messages.parse(output_format=PydanticModel)` 逼模型直接返回校验过的结构化对象（读 `.parsed_output`），而非自由文本再解析。要求 schema `additionalProperties:false`（pydantic `extra="forbid"` 即满足）。支持 Haiku 4.5+/Opus/Sonnet/Fable。
- **强制工具调用取结构化输出（forced tool call）** — 跨 provider 通用的结构化输出做法：把目标 pydantic 模型的 JSON Schema 当成一个工具的 `input_schema`，用 `tool_choice={"type":"tool","name":...}` 强制模型调用它，再从 `tool_use` block 取参数并 `model_validate`。相比各家原生结构化输出（Anthropic `output_format`、OpenAI `json_schema`）**可移植性强得多** —— 兼容端点常常照收原生参数却不遵守。代价：强制只保证"调了工具"，不保证参数合法，仍要自己校验。
- **OpenAI/Anthropic 兼容端点（compat endpoint）** — 第三方模型服务模仿主流 API 格式，让你换个 `base_url` 就复用官方 SDK（如 DeepSeek 的 `https://api.deepseek.com/anthropic`）。坑：**兼容是部分兼容**，新特性（结构化输出、思考模式与 tool_choice 的组合）常静默降级而非报错，必须实测每个用到的参数；且**别用它教的 `ANTHROPIC_BASE_URL` 环境变量**，同机的 Claude Code 等工具也读它，会被一起劫持。
- **judge 多样性（模型多样性 vs 提示词多样性）** — 多 judge 投票只有在投票者**独立**时才有意义。同族模型的误判高度相关，投两次约等于投一次；要多样性就换训练谱系（如 Anthropic + DeepSeek），且**共用同一份提示词** —— 否则分歧分不清是"模型看法不同"还是"问题问得不一样"。
- **判定档位（judgment granularity）** — LLM-as-judge 的输出类别数。**档位不够时模型不会报告"无合适选项"，而是硬塞进现有某一档**，污染下游严重级。定档位是产品决策不是工程细节：判据应从真实数据的错误分布反推，而非先验拍脑袋。配套原则：ground truth 若只收极端案例，就永远测不出边界在哪。
- **跨层守恒断言（cross-layer conservation invariant）** — 把「没有静默跳过」这类原则写成可执行断言：**最终产出 + 全部丢弃记录 ≡ 全部输入**（合并类操作要单独计入，别写成永远对不上的式子）。价值在于它能抓住**逐层审查看不见的接缝缺陷**——每层各自记账都正确，但上层的预过滤让下层的记账代码永不执行时，只有守恒式会失败。写这类断言必须验证它「有牙齿」：故意制造一处静默丢弃，看它会不会 FAIL。
- **审查单元 vs 缺陷单元** — 多 agent 开发里的结构性盲区：逐任务审查的拦截形状与「单层内部缺陷」高度吻合（拦截率近 100%），但**接缝缺陷不在任何一层里**，靠加密审查频次补不上，必须换审查单元（插入专门的"接缝审查"，或做整分支审查）。实测分布：逐任务审查拦下 6 个单层缺陷，全分支审查另发现 4 个 Critical 全在接缝。
- **fake 喂完美输入（happy-path fake）** — 单元测试里每层的 fake 只提供本层的理想输入（不返回越界值、不返回重复项、判定器全部点头），导致「部分成功 + 部分失败」这一真实运行常态**完全无覆盖**。是分层流水线测试套件最常见的系统性盲区。
