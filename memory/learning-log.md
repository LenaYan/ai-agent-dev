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

## 2026-07-27（下半场）— 真实课标语料上量：判定档位与并发瓶颈的连环暴露
- **一句话**：拿真实课标原文（28 条）第一次上量，连着挖出两个此前看不见的问题 —— fidelity 二值判定淘汰 62%、边审核串行撞超时；修完节点 27→64、耗时从 49:32 超时崩溃降到 **2:27**。
- **拿数据的过程本身有坑**：教育部官网《义务教育数学课程标准（2022 年版）》PDF 是**扫描件**（Adobe Image Conversion，无文字层），`pdftotext` 提取结果为空 —— 只能逐页视觉读。40MB / 189 页，小学·数与代数【内容要求】在正文 p.17–26。
- **顺手补了自己捅的闸门漏洞**：`.gitignore` 注释写着"仓库公开，不做对外分发"，但 `data/source/example.md`（真实课标条目）一直被 git 跟踪。**自己定的闸门自己漏了**，且是在往里加语料时才发现。已收口成 `data/source/` 整目录忽略。另：说明文件不能放在语料目录里 —— `split_source` 的 `*.md` glob 会把 README 当语料切一遍（实测 17 条噪声）。
- **① 「判定档位数是产品决策」的第二次实证，且证据比第一次硬**：72 个 draft 里 45 个被 fidelity 淘汰（62%），而这 45 条里 **21 条（47%）是分歧淘汰**。分歧的形状才是关键 —— 原文「理解数位的含义」→ 描述列举各数位，flash ✅ / pro ❌；原文「会比较万以内数的大小」→ 描述"从高位到低位逐位比较"，flash ❌ / pro ✅，**方向是反的**。不是两个模型各有稳定立场，是这个判断在二值框架下**没有稳定答案**，模型每次随机塞格子。上次（name/desc judge）靠真实数据反推，这次直接看到两个模型在同一灰区互相打架。
- **② 更根本的矛盾：忠实性与有用性在纲领性文本上直接冲突**。课标条目是纲领性的（"理解数位的含义"六个字），而知识图谱节点必须可教、可测。要求 description 严格忠于 `source_span`，等于要求节点停留在纲领的抽象层级 —— 那样的节点没法用。二值判定表达不了这个冲突，三档（faithful / reasonable_elaboration / fabricated）能。改完误杀 45→0，draft 存活 27→64。
- **③ 一次失败的调优比成功的更值钱**：为压掉 2 条漏报加了规则"多出来的部分若够格当另一条课标条目 → fabricated"，**实测更差（漏报 2→3）** —— 内容缺失的样本反而被放行。已回退并保留失败记录。它暴露出**忠实性有两个正交维度：多写（编造）与少写（缺失）**，三档只覆盖了"多写"。这不是调 prompt 能解决的，是档位维度缺一个。
- **④ 闸门要按代价不对称来定，不是按准确率**：评测 17/20=85%，误杀 0、漏报 2。误杀让图稀疏到没法用（已实测：62% 淘汰→347 条边只活 6 条）；漏报只是让个别编造节点混进一张 `review_status=unreviewed`/`confidence=0.0` 的图 —— 那正是 provenance 诚实性设计要兜的场景。于是退出码改成"误杀非零即红，漏报记基线（=2）、变差才红"。**基线是已知限制的显式记录，不是目标。**
- **⑤ 一次真实超时同时引爆三条只活在文档里的结论**：三档把 draft 救活后，边审核工作量从"几乎没有"（此前 292 条边在审核前就随端点连坐死了）变成 690 次串行调用，撞上 `NodeTimeoutError: Node 'review' exceeded its run timeout of 600.000s`，总耗时 49:32。三条一起兑现：**(a)** `NODE_TIMEOUT` 第一次在真实运行里打响（此前只有人造测试）；**(b)** `RetryPolicy` 重试了 `NodeTimeoutError`（它既不在 `PROGRAMMING_ERRORS` 也不是 `ValueError`），整层重跑 3 次白烧三倍调用 —— I1 注释里"粒度是整层"的第一次真实代价；**(c)** 49:32 ≫ 10 分钟，印证"timeout 只改变返回结果、不提供墙钟上界"。
- **⑥ 一个被混淆多时的概念**：`review_edges` 需要全局 `kept_ids` 才能**开始**，但开始之后每条边的判定彼此独立。**"需要全局视野"阻止的是扇出（Send 到条目级），不是层内并发。** 这两件事此前一直被当成一回事，所以边审核从来没并发过 —— 只是以前它没活干，没人发现。并发化后 49:32 超时 → **2:27**。**推论：并发校准只覆盖扇出点，不等于整条流水线都并发了**（`dedupe` 至今仍串行）。
- **并发化必须守住的三条**（都是差点踩的）：产物顺序须与串行完全一致（两引擎对等性测试逐字节比对落盘产物，完成顺序泄漏进排列会让它变成随机红绿）；异常须在工作线程就地转成返回值（`executor.map` 惰性，一抛出后续结果就取不到，"单条失败不中断整批"会静默失效）；`PROGRAMMING_ERRORS` 须显式还原类型 raise（线程池会包装异常，这道防线在并发路径上否则失效）。
- 产出：`docs/mcp-server-design.md`、`docs/source-corpus.md`、`docs/error-taxonomy.md`、`docs/concurrency-calibration.md`、`pipeline/concurrency.py`、`errors.py`、fidelity 三档 + `data/fidelity-eval-groundtruth.json`(20 条，全部取自真实判定) + `scripts/eval_fidelity.py`、`scripts/calibrate_concurrency.py`；284 测试。全程 TDD。
- **图的现状（如实）**：64 节点 / 31 边 / 41% 孤立 / **最长前置链只有 3 层**，且那条链三个节点全在 G1、名字高度同义 —— 更像同一件事被切成三个节点，不是真实先修链。跨学段的链（整数→小数→分数→比和比例）没连出来。诊断线成立（75 条 misconceptions、126 条 evidence），规划线"能跑但薄"。
- **下一步（断点很清楚，可直接接手）**：MCP server 尚未写一行代码，设计已定稿在 `docs/mcp-server-design.md`，无阻塞项。
  1. 先写 `src/cn_curriculum_graph/serve/query.py`（纯函数领域层，**不 import 任何 mcp 符号**）：`load_graph` + 索引 + 六个工具的实现（`match_misconceptions` / `search_topics` / `get_topic` / `get_prerequisites` / `plan_path` / `get_graph_stats`）。全 TDD，fixture 图必须覆盖：带环、soft 边、revisits、孤儿、同名节点、空图。
  2. `plan_path` 的 `known_ids` 语义已定死：传入 id 视为已掌握，**它自己和它的全部上游前置一并剔除**（设计文档 §3 有论证，别改成只剔除自身）。
  3. 再写 `mcp_server.py`（`mcp==1.28.1` 的 FastMCP，十几行转发）。**v2 稳定版目标日期是 2026-07-28，届时加第二个绑定做对比，领域层与测试一行不动。**
  4. 诊断评测：`data/diagnosis-eval-groundtruth.json` >=16 条 + `scripts/eval_diagnosis.py`，阈值 `recall@3 >= 0.75`。样本要含同义换词、跨年级同主题、以及**应当召回不到任何东西**的样本。
  5. 验收不是测试绿，是接进 Claude Code 真跑两段对话（诊断 + 规划），看 `misconceptions`/`evidence` 有没有真的进入 agent 的回答 —— 产出一段实录 + 诚实评价，包括"某字段其实没被用上"这种结论。
- **已知未修（都不阻塞下一步，但别忘了）**：①`dropped.json` 跨运行累加从不清空，报告系统性虚高；修法要绕开 checkpoint 重入对 append 幂等的依赖，得配测试。②`source_span` 被抽取层截窄，导致 fidelity 拿到残缺上下文（fidelity 评测里那条"计算器"漏报就是它造成的）。③`dedupe` 仍是串行，是下一个并发瓶颈候选。④边质量：31 条边里最长链只有 3 层且疑似同义节点串，跨学段前置没连出来 —— 独立课题，涉及 edges 层剪枝策略。

## 2026-07-27 — 接真模型上量前的三件事：三条原判断全被自己推翻
- **元收获（比三条技术结论都值钱）**：这三件事都是上一轮实验**自己写进待办**的"已知技术债"，条条言之凿凿。真去还债时，**三条的前提全部不成立**。共同的病根：待办是在**读到一句警告 / 想到一个理论风险**的当下写的，没有一条是"实测到症状"之后写的。教训不是"别写待办"，而是**待办要标清"这条是实测出来的还是推断出来的"**——推断类待办在动手前必须先花十分钟证伪，否则会照着一个错误前提去改，而"照错误前提改出来的东西"可能比不改更糟（第 ③ 条就是活例子）。
- **① `ValueError` 不重试的误伤面**：原以为要收窄排除集合。全量走完 13 处抛出点后发现——**误伤面今天是零，但零是"恰好"来的**：靠六层函数 catch 边界的当前形状，不是任何结构约束。而**收窄是错的方向**：可达 `retry_on` 的 `ValueError` 不止空 judges 哨兵，`io.append_drops` 直接在四个挂了 retry_policy 的 Node 体里 `json.loads` dropped.json，`JSONDecodeError`/`UnicodeDecodeError` 都是 `ValueError` 子类且都可达，收窄会把这些**确定性**错误变成可重试的，纯亏。**真正的病根是那五处"模型未调用工具"用错了类型**——那不是值错误，是远端服务的协议违约，与限流/超时同类。修法是把它搬出 `ValueError`（新增 `ToolCallMissingError(RuntimeError)`），于是"`ValueError` = 确定性错误 = 不重试"从**碰巧成立**变成**按构造成立**。
- **② 并发上限校准**：原默认值 8 的注释写着"保守起点"。查官方文档发现 **DeepSeek 不设 RPM/TPM，只设账户级并发连接数 2500(flash)/500(pro)**——8 低了两个数量级，**它保守的是一个根本不紧的约束**。全档爬坡 0 个 429。真正的天花板是 `asyncio.to_thread` 的**默认线程池 `min(32, cpu+4)`**：并发设 32，实测在飞峰值只有 20。**而这道墙是我们自己为了让 `NODE_TIMEOUT` 生效而引入的**（`async def` 必须真 `await`，于是所有阻塞调用被扔进线程池）——超时能力与并发天花板是同一个决定的两面，此前从没被联系起来看过。吞吐拐点正好压在这道墙上（19 → 12.60 req/s；24 → 12.09 且 p95 +47%，纯排队）。默认值改成公式 `min(32, cpu+4)`，而不是抄下当天那台机器上的 19。
- **③ `allowed_msgpack_modules`**：原判断"未注册，升级 langgraph 会让 checkpoint 整个失效"。**两处都不成立**：(a) 框架 compile 时**已经自己**从 state schema 递归推导 allowlist（`_serde.build_serde_allowlist`），11 个自定义类型一个不落——手写一份反而更差，它会和 schema 漂移；(b) 未注册的后果不是"失效"，是**静默把 pydantic 模型降级成裸 dict**（ext hook 拒绝时 `return tup[2]` 返回 kwargs），症状是下游一句 `AttributeError: 'dict' object has no attribute 'draft_id'`——**看起来完全像业务代码的 bug**，排查方向从一开始就是错的。**真正的坑在两个独立开关的组合上**：自动推导被 `if STRICT_MSGPACK_ENABLED:`（环境变量，import 时读死）门控，而"序列化器严不严格"是另一回事；`严格 serde + 未设环境变量` 这一格**比什么都不做更糟**（严格拦截照常生效、推导整个跳过）——这正是我第一版修复踩中的，续跑当场炸。最终不依赖环境变量，自己调那份推导函数显式 `with_allowlist`。
- **顺带发现的空档**：校准出一个默认并发值，才发现 `--engine` 根本没有扇出版选项——`max_concurrency` 此前只有实验脚本和测试碰得到。**一个调好的参数如果没有生产路径能用上，调参就只是自娱自乐。** 已补 `--engine langgraph-fanout` + `--max-concurrency`（配另外两个引擎时当场退出而非静默忽略）。
- 产出：`errors.py`、`docs/error-taxonomy.md`、`docs/concurrency-calibration.md`、`scripts/calibrate_concurrency.py`、`graph.py` 的 `build_checkpoint_serde`/`open_checkpointer`/`apply_state_allowlist`、`tests/pipeline/test_checkpoint_serde.py`、`tests/test_error_taxonomy.py`、`tests/test_calibrate_concurrency.py`、`tests/pipeline/test_run_cli.py`；276 测试（+37）。全程 TDD。真实端到端跑通（fanout + 严格 serde + checkpoint，3 条课标 → 4 节点，55s，0 error）。
- 下一步：**MCP server，把图暴露给 agent** —— 从"造数据"切到"用数据"，反过来验证 schema 里 `misconceptions`/`evidence` 这些字段到底能不能被用起来。

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

## 2026-07-27 — LangGraph 重写编排层 + 手写 vs 框架受控实验（路线图阶段四验收）
- **为什么现在做**：手写版已跑通**且带着真实失败模式**（judge 抛 429 炸全批、中断只能从头烧钱）。空着手写状态机学到的只有 API；带着具体痛点写，才知道 checkpointer 到底解决了什么、代价是什么。
- **做法**：六层纯函数一行不改，A 阶段 `graph.py`（一层一个 Node）与手写版并存且行为对等，B 阶段 `graph_fanout.py` 用 `Send` 扇出到条目级。故障注入靠包裹已有的 `PipelineDeps`（当初为可测性做的 DI 原封不动变成了实验装置）。239 测试。
- **核心结论（全部实测，多数对框架不利）**：①**Node 级 `RetryPolicy` 对「LLM 偶发失败」不可达** —— 六层纯函数已把非程序错误吞成 `DropRecord` 从不上抛；能触发时**粒度是整层**（实测 4 chunk 故障在层末尾 → 该层调用 8 次），**手写版的逐条 try/except 在这个粒度上严格更优**。②**`NODE_TIMEOUT` 原本永远打不响**：`async def` 但函数体不 `await`，同步阻塞占住事件循环，看门狗拿不到调度；改 `await asyncio.to_thread` 后生效，但**只改变返回结果、不提供墙钟上界**（`asyncio.run` 收尾 join 杀不掉的线程，`THREAD_JOIN_TIMEOUT=300`）。③**checkpoint 收益 = 崩溃点上游的全部调用量**，随规模放大（n=3/10/20 净优势 3/14/44）；但整层确定性失败时 `RetryPolicy` 是**纯亏损**（重烧 3 遍），故障注在层中间而非层入口时净优势降到 1/6/26 —— 小规模下两者几乎抵消。④**编排代码量 1.65×（机制）/ 2.3×（含重入）**。⑤唯一低估框架处：`Send` 扇出真并发带来 **8× 墙钟加速**（8.08s→1.05s），fake extractor 瞬时返回把它抹平了；反面是并发无上界 → 真实场景 429 风暴 → 被逐条 try/except 安静吞掉。
- **框架强加了什么**（比"省了什么"更值得记）：checkpoint 重入逼你把持久化做成幂等，**还逼你把整个 state reducer 做成幂等**；msgpack 自定义类型未注册（未来版本强制）；异步传染（为让 timeout 生效四个 Node 必须 async，`SqliteSaver` 换 `AsyncSqliteSaver`，`asyncio.run` 使它不能从任何已有事件循环调用）；`retry_on` 默认会重试 `ValueError`，而项目刻意的"配置错误当场炸"哨兵全用 `ValueError`。
- **最贵的一条教训（关于我们自己）**：一份声称"两个实现行为对等"的笔记，其 14 条对等性测试 + 4 条逐字节比对**全部跑在 `checkpoint_db=None` 下** —— **我们的对等性测试恰好绕开了框架唯一有价值的模式**。最终审查的 Critical（跑完的 thread 再跑 → state 累加 → 产物被摧毁，且这是生产 CLI 默认路径）正是从这个缺口长出来的。
- 产出：`pipeline/graph.py`、`graph_fanout.py`、`faults.py`、`scripts/compare_orchestration.py`、`docs/langgraph-vs-handwritten.md`（三章：对等对比 / 额外解锁 / 什么时候不该用它）。
- 下一步：接真模型上量前必须重评 `ValueError` 不重试的误伤面、并发上限按实际配额校准、注册 `allowed_msgpack_modules`。

## 2026-07-26 — 生成流水线（六层纯 DAG 工作流）+ 多 agent 驱动开发实践
- **产品/工程收获**：①**六层里三层不需要模型**（切分、组装是纯规则，去重是规则配对+LLM 确认），这个比例本身是设计质量信号，对应 effective-agents 心法①"不要什么都做成 Agent"。②**剪枝规则由校验规则反推**：`GRADE_INVERSION` 会拒绝"前置年级晚于后继"，那就不生成这类候选——省调用是次要的（N²→N），主要价值是**生成端不产出校验端注定要拒的东西**，同一套约束写两遍是 bug 温床。③**模型只产出它有资格产出的字段**：`id`/`provenance`/`standard_codes` 由代码填，`provenance.confidence` **恒为 0.0**——没有教研审核时任何非零置信度都是自欺，这正是 Marble 最大的信任缺口。把这条做成**结构约束**（`DraftContent` 类就是给模型的 input_schema，装不下那些字段）而非约定。④**课标编号必须在切分层绑定**：`LOW_STANDARDS_COVERAGE` 是带硬阈值的 ERROR，抽不到编号足以让整批产出被自己的 CI 拒掉；编号在原文里有格式，属纯规则能干的活，交给模型平白引入不确定性。
- **最重要的一条教训 —— 原则会在接缝处被稀释**：六层各自都老实记了 `DropRecord`，`review.py` 甚至为丢边写了完整的 `UNKNOWN_REVIEW_TARGET` 记账，但**编排层一行 dict comprehension 的预过滤让那段代码永不执行**，真正丢边处一声不吭。逐层审查看不见它，因为它不在任何一层里。修法之外更重要的是：**把原则写成跨层守恒断言**（最终产出 + 全部 DropRecord ≡ 全部输入），让"没有静默跳过"从口号变成会失败的测试。
- **测试盲区**：159 个测试里每层的 fake 都给自己喂**本层的理想输入**（extract 的 fake 从不返回倒挂年级，edges 的 fake 从不返回重复边，端到端的 fake 让每个 judge 都点头），**没有一个跑过"部分成功+部分失败"**——而那才是真实运行的常态。四个 Critical 全落在这个盲区。
- **多 agent 驱动开发的实测经验**（8 任务 × [实现 agent + 独立审查 agent]）：①**拦截效果分布很说明问题**：逐任务审查拦下的 6 个缺陷**全是单层内部的**（CRLF 泄漏、恒真断言 `assert schema == schema`、三方同名裸名逃逸、`all([])==True` 零判定器满票、共享 `Provenance` 引用、空产出静默绿灯）；最终全分支审查发现的 4 个 Critical **全在接缝上**。**审查单元 = 任务单元，而缺陷单元 = 接缝**，靠加密审查频次补不上，得换审查单元。②**subagent 的自述必须独立验证，包括它对规格的引用**：两次出现实现者给自己的判断加引号、包装成"brief 明确要求的"，而该句在任何文件里检索不到。技术判断本身都对，是表达层的奖励黑客（给自己的决定找权威背书）。已固定在派活提示里加"凡引号引用必须原文可检索"，审查者每轮实地核查。③**比虚构引用更严重的是"知情不修"**：一次实现者在报告里**准确预判了 Critical**（连触发路径都写对）却只写成待办就提交——虚构引用会被检索抓住，知情不修只会静静躺在报告里。规则应是：报告里出现"bug/缺陷/会崩溃"措辞的，要么当次修掉，要么提交前升级裁决。④**审查者故障时要重派，不要自己顶替**：唯一缺失独立审查的那一层（reviewer 两次基础设施故障，我自己写探针顶上），正是最贵的两个缺陷所在地——自己写的探针只覆盖自己想到的不变量。
- 产出：`practice/cn-curriculum-graph/pipeline/`（六层 + 编排 + `ccg-generate` CLI）、`docs/pipeline-design.md`、`docs/pipeline-implementation-plan.md`；159 测试；已接 deepseek-v4-flash 端到端跑通（3 条课标 → 94 秒 → 4 节点，0 error）。全程 TDD。
- **首次真实运行的发现**：fidelity judge 第一次跑就抓到模型在描述里编造了原文没有的"十进制分数/位值"概念（这一层的存在价值当场兑现）；但**淘汰会制造孤儿而流水线无感知**（基础节点被淘汰，后继静默失去前置）；**同年级互为候选产出双向边**，靠 judge 兜住而非剪枝挡住（剪枝没反推 `CYCLE`）。
- 下一步：接真模型上量前必须先修——剪枝反推 CYCLE、淘汰孤儿感知、`run_all` 误报 `CONSISTENCY_SKIPPED`（review 层明明跑过 name judge）、补重试退避与 `--from` 重入、`candidate_pairs` 的 O(n²)。

## 2026-07-26 — 判定标准从二值扩到三档：档位数是产品决策（TDD）
- 收获：①**判定档位不够时，模型不会告诉你"没有合适的选项"，它会硬塞进现有某一档**。二值 judge 在 8 条手写 ground truth 上 100%，一上 Marble 真实数据就暴露出第三类情形：名称与描述**同属一个主题但覆盖范围对不上**（Deep-Sea Survival 的描述扩到木蛙和水熊虫）。这类被硬塞进"不符"，于是 ERROR 里混进大量本该是 WARNING 的东西。**改成三档判定（consistent / scope_mismatch / topic_mismatch）→ 两级严重性（无 / WARNING / ERROR）后，ERROR 从外推的约 140 条降到真实的 28 条。**②**ground truth 的样本分布决定了你能发现什么**：手写样本全是"乘法 vs 除法"这种泾渭分明的，压根测不出边界在哪；必须拿真实数据反哺 ground truth（扩到 16 条，含 6 个边界案例 + 1 条初版误判的对抗样本）。③**schema 用 Literal 而非 Enum**：pydantic 把 Literal 内联成 `{"enum":[...]}`，Enum 则生成 `$defs` 引用——各家结构化输出/工具 schema 对 `$ref` 的支持参差不齐，内联更安全。④**LLM 生成的知识图谱，同名节点是高危区**：全量 1590 节点跑完 28 条 ERROR 里 **86%(24/28) 落在跨年龄段复用的同名节点**上（`Understanding angles` 一名七用，其中 4 个描述分别在讲长方形面积/四边形分类/尺规作图/长方形性质）。不是随机噪声，是"名称当主题族标签复用、描述来自各年龄段具体课标条目"导致的逐级漂移——本项目的生成流水线要专门防这一手。
- 全量实测（deepseek-v4-flash，10 并发，205s，0 失败，约 $0.08）：consistent 1454(91.4%) / scope_mismatch 108(6.8%) / topic_mismatch 28(1.8%)。16 条 ground truth 上三档查准查全均 100%。
- 产出：`Verdict.judgment` 三值 Literal + `is_consistent`、新增 `NAME_DESC_SCOPE_MISMATCH`(warning)、prompt 教三档区分（含"拿不准倾向 consistent"）、eval_judge 改 3×3 混淆矩阵（漏报/降级才非零退出）、ground truth 8→16 条；58 测试（+3）。
- 下一步：生成流水线骨架（多 agent 抽取→去重→依赖边→交叉审核，先用十几个手写种子跑通链路）。

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
