# Session: Marble os-taxonomy 上游分析（产品/架构视角）

Date: 2026-07-24（分析日）· 迁移日 2026-07-24

> **来源说明**：本文迁移自另一个工作区（`~/Claude/child_watch_baobao`）的会话存档
> `sessions/2026-07-24-marble-taxonomy-analysis.md`。那是个**非 git 目录**，不跨机器同步，
> 而这部分分析是 `practice/cn-curriculum-graph/` 的直接上游依据，因此迁过来。
>
> **只迁与本工作区主线相关的部分**（产品分层、工程设计、数据质量、可复用性）。
> 原文中针对具体家庭/孩子的判断留在原处，不入本仓库。
>
> 定量核对结果与 `name/description` 名实不符的节点表见
> `2026-07-24-cn-curriculum-graph-feasibility-and-scaffold.md` 第一节，此处不重复。

---

## 一、这是什么

[withmarbleapp/os-taxonomy](https://github.com/withmarbleapp/os-taxonomy) —— Marble 团队 2026-07
开源的儿童教育知识依赖图数据集。纯 JSON，可二次开发，可商用。
许可：数据库 **ODbL 1.0** + 内容 **CC BY-SA 4.0**（商用不要求开源自己的产品，
只要求把对该数据集**本身**的改进回馈）。

| 文件 | 内容 |
|---|---|
| `topics.json` | 节点：ID、type（conceptual / procedural / representational / language / meta）、subject、domain、名称、描述、年龄段、evidence criteria、assessment prompt、课标对齐、centrality |
| `dependencies.json` | 先修边：hard / soft + **一句人写的理由** |
| `curriculum-standards.json` | 按课标框架分组的标准条目 |
| `clusters.json` | 面向家长的领域摘要 |
| `manifest.json` | 计数、分类统计、SHA-256 校验和 |

**对传播口径的三处纠偏**（社区分享 → 实际）：

1. 年龄范围**不是 6-12 岁**，实际约 4-15 岁（每 topic 带 `ageRangeStart` / `ageRangeEnd`）。
2. 所谓"8 个学科"**密度高度集中在理科**：Science 547 / Math 503 / English 286 /
   History 90 / Personal & Social 88 / Life Skills 37 / Computing 21 / Learning to Learn 18。
3. **最关键、传播中完全没提的一点**：README 明确写了 **"data only"** ——
   不含 learner model、不含掌握度追踪、不含自适应算法、不含 embedding。

---

## 二、核心判断：它是一张地图，不是导航系统

"AI 教育的创新不在接新模型，在于调度定位"这句话是对的。但把这个**开源数据集**
当成"调度定位"的实现，是错的：

| 层 | 类比 | Marble 开源了吗 |
|---|---|---|
| 知识依赖图（DAG + 元数据） | 地图数据 / OSM | ✅ 开源了 |
| 学习者状态模型（掌握度、遗忘、置信度） | GPS 定位 | ❌ 没有 |
| 路径规划与调度策略 | 路由算法 | ❌ 没有 |
| 内容生成 + 评测 | 语音播报 | ❌ 没有（自己接 LLM） |

传播中描述的产品体验（选起点 → AI 顺先修关系推小块知识 → 学完重算路径 → 家长每日邮件），
是他们**闭源商业 App** 的形态，不是这份数据集能做的事。

开源的是产品里**最容易被复制、最不构成壁垒的那一层**；真正的 know-how
（learner model 怎么建、mastery 怎么判、路径怎么选）全留在闭源侧。
标准的开源策略：开源数据吸引生态、把地图做成事实标准，然后卖导航。

> **对本工作区主线的意义**：缺的那三层（状态建模 / 调度策略 / 生成+评测）
> 恰好就是 **agent 要做的事**。这份数据集因此是一份现成的、真实的"环境"，
> 可以在其上研究「agent 如何在结构化知识上做定位与调度决策」。

---

## 三、真正有价值的设计（工程视角）

抛开叙事，两个设计确有含金量：

**1. 边上带 hard/soft + 人可读的理由。**
传统知识图谱的边是无语义的 `prereq(A, B)`。这里给每条边打了强弱并附一句自然语言解释。
真实目的是**给 LLM 消费** —— 拿到"学分数加法前要会等分，因为分母代表的是等分的份数"
这类理由，可以直接编进 prompt 做讲解和诊断。
**这是"为 LLM 而设计的知识图谱"，跟 2015 年那批为规则引擎设计的图谱不是一回事。**

**2. 每个节点带 evidence criteria + assessment prompt。**
即"怎么算掌握了"和"用什么问题去验"。这是整个数据集商业价值最高的部分：
把本来需要教研团队产出的**评测设计**，模板化成了 LLM 可执行的指令。

两点合起来，本质是**把教研资产翻译成了 LLM 的调用协议**。这才是真正的创新点。

**被高估的部分**："知识图谱做个性化学习"这个概念一点都不新 ——
Knewton、松鼠 AI（号称拆到"纳米级知识点"）十年前就在做，更早还有 Cognitive Tutor / ALEKS。
它更现实的归宿是：教育科技创业公司的**冷启动数据集**，省掉 6-12 个月的教研搭建。

---

## 四、必须先验证的坑

**① 数据产出方式没有交代。**
`PROVENANCE.md` 通篇讲授权与许可（NGSS / C3 / IB PYP 因版权只给标准编号不给原文），
**完全没有说明生产方法，也没有任何 validation / QA 流程描述**。
1590 节点 + 3221 条带理由的边，纯人工成本极高，大概率是 LLM 大规模生成 + 人工抽检。
这本身没问题，但**没有公开校验方法就是红旗**：依赖图里一条错边会把整条学习路径导歪，
且错误是**静默的**，使用者发现不了。

→ 这条直接决定了 `cn-curriculum-graph` 把 `MISSING_PROVENANCE` 定为 **ERROR 而非 WARNING**，
以及 `CONSISTENCY_SKIPPED` 这条"跳过必须留痕"的规则。

**② DAG 这个建模本身是有损的。**
真实学习不是 DAG：会遗忘、会螺旋上升（分数在 3/4/5/6 年级各教一轮、难度递进）、会横向迁移。
DAG 只建模了"顺序"，没建模"熟练度衰减"与"部分掌握"。
真正难的恰恰是被略过的那部分（BKT / DKT / IRT 一系），
而那部分需要**大量真实作答数据**才能标定 —— 开源不了，也正是他们的护城河。

→ 这条直接催生了 `cn-curriculum-graph` 的 **`revisits` 螺旋边**设计。

**③ 对中国场景，可用面比看上去小得多。**
- English 286 个节点是**英语母语的读写体系**（phonics、sentence building），国内不适用
- History 90 是英美史观；Science 547 对齐 NGSS，与国内科学课差得更远
- Math 503 可用，但**序列与人教版课标不同**（统计概率引入时点、代数思维铺垫方式差异明显）
- **没有中国课标对齐 —— 这是硬伤**

真正可迁移的大致是**数学的依赖结构 + 科学的部分概念网**，且需自己做课标映射。

---

## 五、核对后的一项额外发现：课标年龄错位（真实差异，非数据错误）

Marble 把以下人教版**四年级**内容标为 **10-11 岁**（对应英国 Y6 ≈ 国内五六年级）：

- 四则混合运算顺序
- 除数是两位数的除法
- 三角形内角和
- 平均数
- 鸡兔同笼类问题

→ **人教版在这几个点上比英美课标早约一年。**

**对 `cn-curriculum-graph` 的含义**：
① 印证了不能直接用年龄做进度基元 —— 项目改用**年级（1-9）**是对的；
② `adapters/marble.py` 的年龄→年级映射不仅"有损"，还系统性偏移，
其产出**只能用于验证校验层**，不能当中国课标数据；
③ `GRADE_INVERSION` 规则在跨课标数据上必然噪声偏高，是设计时就该预期的，不是 bug。

---

## 六、对主线的价值：可用的实验素材

- 少见的、高质量的、结构化的领域知识图谱（1590 节点 / 3221 带理由的边）
- 社区已有 MCP server：**`taxonomy-mcp`**，可直接挂进 agent（未亲测）
- 比玩具数据集真实得多 —— 适合研究"agent 在结构化知识上做调度决策"
- 许可友好：ODbL + CC BY-SA，商用只要求回馈对数据集本身的改进，**不传染产品代码**

---

## Action Items

- [ ] 把 `os-taxonomy` + `taxonomy-mcp` 正式纳入本工作区实验素材
      （现在只在 `practice/cn-curriculum-graph/README.md` 里作为 clone 步骤出现，
      没有独立的 MCP 实验；`taxonomy-mcp` 尚未亲测，接入前先核实其时效与实现质量）
- [ ] 若要在其上做调度实验，**缺的是 learner model** —— 这层必须自己写，
      是"agent 在图上做决策"最值得练的部分

---

## Sources

- [withmarbleapp/os-taxonomy — GitHub](https://github.com/withmarbleapp/os-taxonomy)
- [os-taxonomy PROVENANCE.md](https://github.com/withmarbleapp/os-taxonomy/blob/main/PROVENANCE.md)
- [The Atlas of Learning: Marble Skill Taxonomy — DAIR.AI Academy](https://academy.dair.ai/resources/marble-skill-taxonomy)
- [taxonomy-mcp（社区 MCP server）— LobeHub](https://lobehub.com/mcp/kylelynch-taxonomy-mcp)

> 时效标注：以上核实于 **2026-07-24**（数据版本 v1，生成于 2026-07-08）。
