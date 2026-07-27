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
