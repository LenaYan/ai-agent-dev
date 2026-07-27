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
