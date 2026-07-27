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

## ADR-0002 — 采用《构建有效 Agent》三原则为工作区默认心法  (2026-07-06)
- 背景：需要一套贯穿全程、不随框架过时的 Agent 设计判断准则。
- 决定：采用 Anthropic（Barry Zhang & Erik Schluntz）三原则：①不要什么都做成 Agent ②尽可能久地保持简单 ③像你的 Agent 一样思考。
- 理由：一手权威源；反炒作、聚焦工程取舍；对资深工程师迁移价值高。
- 取舍/放弃项：Anthropic 有产品立场（强单模型+工具），"少用多 Agent"部分契合其叙事——已在文档中标注保留，不盲从。
- 影响：已在 AGENTS.md、docs/roadmap.md 引用；设计任何 Agent 默认遵循。详见 docs/effective-agents-principles.md。

## ADR-0003 — 工作区同时支持 Copilot CLI 与 Claude Code  (2026-07-06)
- 背景：希望同一目录既能用 Copilot 也能用 Claude Code，且不重复维护规约。
- 决定：以 AGENTS.md 为单一事实来源；新增 CLAUDE.md 用 `@AGENTS.md` 导入供 Claude Code 读取。
- 理由：Copilot 已直接读 AGENTS.md，Claude Code 默认只读 CLAUDE.md；导入方式让两者共享一份、几乎无重复。
- 取舍/放弃项：放弃 symlink 方案（Copilot 会重复载入同一文件）与双份全量文件（必然漂移）。依赖 Claude Code 的 @import 语法，未来若变需调整。
- 影响：改规约只改 AGENTS.md；工具专属配置分别放 .github/ 与 .claude/。

## ADR-0004 — 双工具对称性推进到路径级规则与记忆治理  (2026-07-06)
- 背景：ADR-0003 的单源架构只覆盖全局规约。路径级规则（`applyTo` frontmatter）是 Copilot 专属机制，对 Claude Code 不生效；且三套记忆（工作区 memory/、Copilot `/memory`、Claude Code 自带持久记忆）没有分工规则，存在"两个工具记得的东西发散"的风险。
- 决定：① `samples/`、`practice/` 各放一个导入桥 CLAUDE.md，指向 `.github/instructions/code.instructions.md`；② 新增项目级 `.claude/settings.json`，白名单放行 uv/python/pytest 等验证类命令；③ `memory/README.md` 明确"项目事实只写工作区 memory/，工具原生记忆只放工具偏好与指针"。
- 理由：延续 ADR-0003 的"内容单源 + 工具侧薄适配"模式；权限白名单让"动手即验证"原则不被权限提示打断。
- 取舍/放弃项：权限配置无法跨工具共享，接受两边各配一份；暂不固化自定义命令/skills（等某流程真的重复三五次再做，遵循"尽可能久地保持简单"）。
- 影响：路径级规则改动仍只改 code.instructions.md；新增 `.claude/` 目录入库（settings.local.json 仍被 gitignore）。

## ADR-0005 — 跨机器双工具工作流：Git 为唯一同步通道 + 会话交接纪律  (2026-07-07)
- 背景：工作模式为机器 A 用 Claude Code、机器 B 用 Copilot CLI 交替开发（同为 Opus 4.8）。任何不在仓库内的状态切换即丢失；最大风险是"上午未推送、下午在旧状态上工作"导致 memory 冲突与上下文断裂。
- 决定：在 AGENTS.md §5 增加会话交接纪律——开始 pull + 读 learning-log 最新条；结束更新 memory + commit + push（WIP 也推）；中断任务在"下一步"写清断点；新密钥同步 `.env.example`。
- 理由：learning-log 的"下一步"字段天然是交接文档；WIP commit 的代价（历史不干净）远低于状态丢失的代价。
- 取舍/放弃项：不追求两工具行为一致（system prompt/工具集不同，口吻差异不可消除也无害）；工具私有记忆（Claude 持久记忆、Copilot /memory）接受不同步，靠 ADR-0004 降级为指针。
- 影响：两个工具在会话收尾应主动提醒 commit & push；密钥与 uv 环境需两台机器各配一次。

## 2026-07-24 — 课标知识图项目定位为「流水线」而非「数据集」
- 决策：不做"中国版 Marble 数据集"，做"能生成并自校验知识依赖图的多 agent 流水线"，用小学数学当验证领域。
- 为什么：依赖边正确性没有形式化验证方法（图无环、年级单调 CI 能查，但"A 是不是 B 的前置"只有教过书的人知道）。教研审核是真正瓶颈，靠工程补不上。而流水线是可复用能力，换个领域（法规、内部文档、合规）立刻能用。
- 取舍：放弃"产出可信数据集"这个目标，换来教研审核不到位不再是致命伤，而是一个诚实标注的已知局限（schema 里 Provenance.review_status 默认 unreviewed，MISSING_PROVENANCE 是 CI 硬错误）。
- 按第 3 层次（开源/产品）的标准设计 schema 和 provenance，将来升级不用推倒重来。真要产品化需先解决：教研员审核背书 + 课标著作权法律意见。
- 详见 practice/cn-curriculum-graph/docs/feasibility-analysis.md、docs/schema-design.md（D1-D8 逐条决策）

## ADR-0006 — MCP 暴露层的领域层不放 LLM，检索质量交给 ground truth 说话  (2026-07-27)
- 背景：把知识图暴露给 agent 时，诊断场景要把"孩子说 0.45 比 0.405 小"对到知识点上。直觉方案是在 server 里调一个模型做语义匹配。
- 决定：领域层是**纯检索 + 纯图算法**（零 key、零成本、全可测），语义判断留给消费端；够不够用由 `scripts/eval_diagnosis.py` 的 recall@3 判定，不由直觉判定。
- 理由：①MCP server 的消费方本身就是 LLM，在 server 里再放一个模型是把同一个推理做两遍 —— 心法①要拦的正是这个；②server 一旦要 key，就多出成本、启动依赖、测试要注入 fake 三笔账，而换来的能力消费端本来就有。
- 取舍/放弃项：放弃语义泛化能力。中文同义换词会掉召回，实测 recall@3 = 84%（22 条 ground truth，阈值 75%），剩余未命中集中在"具体数字被换掉"这类。若将来评测明显掉线，这条 ADR 就该被推翻 —— **阈值是判据，不是目标**。
- 影响：`serve/query.py` 不 import 任何 mcp 符号也不 import 任何 judge；绑定层只转发。附带结论：设计时"定 @3 不定 @1"的理由（给 LLM 三个候选自己挑）在当前实现下**不成立**，实测 @1/@3/@5 完全相同，排序维度是空的。
- 验收补记（2026-07-27，两段真实对话）：这条决定**赢了，而且赢的机制与预想不同**。担心的"同义/换数字认不出"由 `misconceptions` 字段本身补上了 —— 家长说 0.45/0.405，命中的是图里的 0.3/0.03，**同型不同数**。**误概念是有限且可枚举的，枚举完就不需要语义模型去泛化。** 可推广的一般结论：当某类知识可以穷举时，"检索 + 数据"能替掉一层模型。
- **向量对比补记（2026-07-27，`practice/cn-curriculum-graph/docs/rag-vs-literal.md`）——"不推翻"这个分支的记录**：
  - 这条 ADR 立的时候留了明确的推翻条件（"若将来评测明显掉线就该被推翻"）。本轮**主动**去撞它：真装了 `sentence-transformers`、真下了 4.3G 的 `BAAI/bge-m3`、只替换 `_relevance()` 内部（聚合权重/语料/ground truth 一行不动），两条路线各扫完 0.05–0.95 的完整前沿，在**空样本正确率 = 100% 这个相同工作点**上读数。
  - **结果：向量没有赢。字面 89%（阈值 0.15）vs 向量 79%（阈值 0.7），且向量的命中集是字面命中集的真子集（零个独赢样本）。** ADR-0006 不推翻，**不新增 ADR-0007**。
  - **推翻条件当时就写好了怎么执行，这里记下来备查**：若向量赢了，需新增 ADR-0007 取代本条 —— 默认打分器换成 `VectorScorer`、`sentence-transformers` 从 optional 升为主依赖、`MIN_RELEVANCE` 改用余弦标度、MCP server 默认路径切向量，并接受"启动加载 2G 模型 + CI 要下模型 + 查询延迟 ×7.5"三笔账。**这三笔账现在不用付，本身就是本轮最大的收益。**
  - **本条 ADR 的取舍项里那个 84% 是错的**：那是生产默认阈值 0.2 上的值；同工作点最优在 0.15 = **89%**。以后引用基线一律用 89% —— 拿 84% 当基线会让任何新方案凭一个被压低的基准"赢"，那正是这个实验最要防的自欺。
  - **"@1/@3 排序维度是空的"这条附带结论要按工作点重述**：@0.2 时 @1=@3=@5=84%（确实全空）；@0.15 时 @1=84%、@3=@5=89%，**出现了 1 条样本的差距**。而向量版 @0.7 是 @1=@3=@5=79%，**排序维度依然全空**。即"给 LLM 三个候选自己挑"这条理由唯一的正面证据来自字面版调低阈值，不是来自向量。
  - **代价（实测，非估算）**：模型 4.3G（其中一半是 `transformers` 5.x 后台从 PR 分支 `refs/pr/130` 另抓的一份 safetensors）｜`.venv` 81M→825M（+744M，29 个包）｜建索引 0.00s→~10.5s｜单次查询 1.6ms→29.7ms（新提问）/12.0ms（复查）。
  - **取舍/边界**：本轮只测了 dense 单路 + 单个模型（bge-m3），没做 hybrid / rerank / 微调 / 查询侧 instruction。结论的正确说法是"在 353 条中文数学术语短文本上，开箱即用的 dense 单路打不过带数学表达归一化的双向 gram 覆盖率"，**不是"向量检索没用"**。且 n=19，10 个百分点只等于 2 条样本（McNemar p=0.5，统计上区分不开）——**能说的是"向量没赢而代价确定"，不能说"字面显著更优"**。
  - **影响**：生产路径一行不动（MCP 仍走字面、`MIN_RELEVANCE` 仍 0.2、聚合权重不动）。`--scorer vector` 作为可复现的对照路径保留（成本只是一个 optional extra）。**换模型是一行**（`--model` 参数）；**hybrid 不是** —— 真正想要的 hybrid 是 RRF 之类的排名融合，需要打分器看到候选集合并产出有序列表，而现协议 `Scorer.relevance(query, target) -> float` 永远只看一对文本，给不了候选集。剩下能做的只有 `max(literal, w*cosine)` 这种分数融合，又撞上余弦与覆盖率标度不可比、单个 `min_relevance` 闸门服务不了两者这个洞见。若要推进，第一步是先扩协议（或换成 `Retriever(query, k) -> list[(id, score)]` 的形状），不是加一个参数。
