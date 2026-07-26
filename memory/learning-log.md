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

## 2026-07-24 — Marble os-taxonomy 上游分析（从家庭教育工作区迁入）
- 收获：①**分清"地图"与"导航"**——这类开源教育数据集只放出知识依赖图（地图），learner model / 调度策略 / 生成评测（GPS + 路由 + 播报）全留在闭源侧。缺的那三层恰好就是 agent 要做的事，所以它是很好的实验"环境"而非成品方案。②真正的创新点是**把教研资产翻译成 LLM 的调用协议**：边带 hard/soft + 人可读理由、节点带 evidence criteria + assessment prompt，为 LLM 消费而设计，与 2015 年那批为规则引擎设计的图谱不是一回事。③**DAG 建模是有损的**：不含遗忘、螺旋上升、部分掌握（BKT/DKT/IRT 那一层需真实作答数据，开源不了）——这条催生了本项目的 revisits 螺旋边。④**课标年龄错位是真实差异**：人教版四年级的四则混合运算/两位数除法/三角形内角和/平均数被标为 10-11 岁（英国 Y6），说明人教版早约一年 → 印证进度基元该用年级而非年龄，且跨课标数据上 GRADE_INVERSION 噪声高是预期行为不是 bug。
- 产出：sessions/2026-07-24-marble-upstream-analysis.md（原始会话在非 git 目录 ~/Claude/child_watch_baobao，故迁入本仓库）
- 下一步：同上条——生成流水线；另可选：把 os-taxonomy + 社区 taxonomy-mcp 纳入实验素材，若要做调度实验则需自写 learner model（缺的正是这层）。

## 2026-07-26 — 多 provider judge：DeepSeek 接入 + 真实数据实测（TDD）
- 收获：①**结构化输出不是可移植特性，"强制工具调用"才是**。DeepSeek 的 Anthropic 兼容端点（`https://api.deepseek.com/anthropic`）能用同一个 anthropic SDK，但**照收 `output_format` 却不遵守**（实测返回自由文本"否"，SDK 在 JSON 解析上炸）；改成「强制调用一个 `input_schema` 就是 Verdict 的工具」即通 —— 任何支持 tool use 的模型都能跑，代价是自己 `model_validate` 一遍。另：DeepSeek v4 默认开 thinking，而**思考模式不接受强制 tool_choice**（400），需显式 `thinking={"type":"disabled"}`；分类任务本也不需要思考预算。②**judge 多样性必须来自模型，不能来自提示词**：两个 judge 共用同一份系统提示（`judges/prompt.py`），否则分歧分不清是"模型看法不同"还是"问题问得不一样"，投票机制失去意义。而 Anthropic/DeepSeek 是不同训练谱系，误判模式不重叠，正好当交叉审核的独立投票者（同族模型误判高度相关，投两次约等于投一次）。③**干净样本会掩盖定义歧义**：8 条手写 ground truth 上 deepseek-v4-flash 拿了 100%，但抽 Marble 真实节点 124 个跑出 11 条(≈9%)，其中 6 条是「名称与描述**覆盖范围**不一致」而非「讲的是不同知识点」—— ground truth 测不出的语义缺口，真实数据一跑就现形。④**环境变量会互相踩**：DeepSeek 官方教 `export ANTHROPIC_BASE_URL=...`，但同机 Claude Code 也读这个变量，export 出去会把 Claude Code 整个劫持走 —— base_url 一律代码里显式传，key 用独立 `DEEPSEEK_API_KEY`。
- 成本/性能实测：deepseek-v4-flash 单次约 1.4s；8 并发跑 124 节点 22s、0 失败。全量 1590 节点约 $0.08（对比 Haiku 约 $0.6）。订阅走 `claude -p` 也能当 judge 且判得对，但单次 26s、烧 26k token（有效输入只有 10）—— agent runtime 的固定开销，批处理场景不成立。
- 产出：`judges/deepseek_judge.py` + `judges/prompt.py`（抽出共用提示）+ CLI `--judge {none,anthropic,deepseek}`（每个 judge 认自己的 key，缺 key 清晰报错退出 2）+ `eval_judge.py --judge`；55 测试（+11）。全程 TDD。
- 下一步：**先定死"范围不匹配算不算名实不符"这条产品语义**（本项目第一个非工程决策），把那 6 个边界案例补进 ground truth 并据此调 prompt；然后才是生成流水线骨架。

## 2026-07-24 — cn-curriculum-graph 接真 LLM judge，激活 NAME_DESC_MISMATCH（TDD）
- 收获：①**judge = 纯函数 (name, description) -> Verdict，Protocol 钉死契约，LLM 只是一个实现**。project 里第一个"模型在循环里"，也是生成流水线"交叉审核"层的前置（那层本质是多 judge 投票）。②**结构化输出用 messages.parse(output_format=Verdict)**，逼模型直接返回 {consistent, reason}，Verdict 的 extra="forbid" 恰好满足结构化输出要求的 additionalProperties:false；temperature=0 求可复现；分类任务默认 Haiku 4.5（便宜、够用，构造参数可覆盖，评测证明不够再升）。③**client 依赖注入是可测性的关键**：构造时可注入 fake client → 单元测试验"接线"（喂对参数、翻译对返回）而零网络零 key；anthropic 懒加载，测试连 SDK 都不碰。④**有 ground truth 才敢信 LLM 判定**：拿手工核对的 4 个 Marble 名实不符节点当标尺，eval_judge.py 算 accuracy/precision/recall，漏报(FN)非零退出——这套 ground truth 基建将来复用给流水线审核层。
- 产出：judges/anthropic_judge.py + scripts/eval_judge.py + data/judge-eval-groundtruth.json + CLI `--judge anthropic`/`--model`；44 测试（+6）。全程 TDD（先写失败测试再实现）。anthropic>=0.69 入 deps。
- 待办：本地拿真 ANTHROPIC_API_KEY 跑 eval_judge.py 量 Haiku 准不准（无 key 时清晰报错退出 2，非 traceback）。
- 下一步：生成流水线骨架（多 agent 抽取课标→去重→依赖边生成→交叉审核，先用十几个手写种子跑通链路）。法律定性只卡"大规模生成+对外发布"，本地自用不受阻。
