# cn-curriculum-graph —— 中国课标知识依赖图（schema + CI 校验层）

## 这是什么

把中国义务教育课标拆成可教、可评的 micro-topic，用先修依赖边连成图，
让 LLM 能在其上做定位与调度 —— 对标 [Marble Skill Taxonomy](https://github.com/withmarbleapp/os-taxonomy)，
但换成中国课标，并修掉原版几个已确认的缺陷。

**当前进度：地基 + 生成流水线 + MCP 暴露层已跑通。** schema 定义、CI 校验层、
六层生成流水线（切分 → 抽取 → 去重 → 连边 → 交叉审核 → 组装）、
把图暴露给 agent 的 MCP server 均已完成，416 个测试，
并已接真模型端到端跑通。**当前一份（2026-07-29）：78 节点 / 213 边 / 4 个孤立（5%）/
最长前置链 12 层**，校验 0 error、4 warning。产出的图数据**不入库**（见下方「许可与来源」），
但**稳定 id 注册表 `data/topic-registry.json` 入库** —— 图不再是源的纯函数，
性质是「同一份源 **+ 同一份注册表** → 同一张图」，注册表不入库等于每次重跑 id 全变
（见 `docs/pipeline-reproducibility.md`）。

> ⚠️ 这个项目的**真正资产是流水线，不是数据集**。
> 数据集的可信度取决于教研专业审核（见 `docs/feasibility-analysis.md` 第二节），
> 那一环靠工程补不上；而"从非结构化文本生成并自校验结构化知识图"
> 这条链是可复用的能力，换个领域（法规、内部文档、合规要求）立刻能用。

## 怎么跑

```bash
uv sync
uv run pytest                        # 416 个测试
uv run ccg-validate data/example-graph.json  # 校验一份图数据（默认跳过语义一致性，留 CONSISTENCY_SKIPPED 警告）

uv run python scripts/export_schema.py   # 重新导出 JSON Schema
```

**MCP server**：把图暴露给 agent（**不需要任何 key** —— 领域层是纯检索 + 纯图算法）。

```bash
uv run ccg-mcp                       # stdio server，默认读 data/generated/graph.json
CCG_GRAPH_PATH=/path/to/graph.json uv run ccg-mcp    # 换一份图

# 检索够不够用，由 ground truth 说了算（28 条样本 = 17 计召回 + 11 应当召回不到，零 key、零成本）：
uv run python scripts/eval_diagnosis.py     # recall@1/@3/@5，低于 75% 非零退出

# 完整阈值前沿（recall@3 × 空样本正确率），不只报一个点：
uv run python scripts/eval_diagnosis.py --threshold-sweep
uv run python scripts/eval_diagnosis.py --min-relevance 0.25   # 逐条明细停在指定工作点
```

**向量检索（可选，实验用）**：把字面打分器换成 bge-m3 的余弦相似度。
**这是对照路径，不是生产路径** —— MCP server 与默认评测仍走字面检索。

```bash
uv sync --extra embed                # 装 sentence-transformers（实测 .venv +744M）
uv run python scripts/eval_diagnosis.py --scorer vector --threshold-sweep
uv run python scripts/eval_diagnosis.py --scorer vector --model Qwen/Qwen3-Embedding-0.6B  # 换模型是一行
# ⚠️ 换成 Qwen3-Embedding 前先看清楚：它官方推荐查询侧加
# "Instruct: ...\nQuery: ..." 前缀，文档侧不加。当前 Embedder 协议是
# encode(texts)，不区分谁是 query 谁是 doc，换这个模型会悄悄拿到偏低的分数，
# 容易被误记成"换个模型也没赢"。
```

> ⚠️ 首次运行会下模型：`BAAI/bge-m3` **实测落盘 4.3G**（其中一半是
> `transformers` 5.x 后台从 PR 分支另抓的一份 safetensors），耗时 >10 分钟，
> 落在 `~/.cache/huggingface/hub`（仓库外）。默认 `uv sync` 不装它，CI 也不装 ——
> **领域层的测试全靠假 embedder 跑，卸掉模型库后 416 个测试一个不掉。**

**结论：向量没有赢。** 同工作点（空样本正确率 = 100%）上
**字面 79%（阈值 0.25）vs 向量 53%（阈值 0.75）**（2026-07-28 把空样本从
3 条补到 11 条后重扫的值；补样本前是字面 89% @0.15 vs 向量 79% @0.7）。
立项时点名"必须上向量才能救"的三条样本，向量一条也没救回来。
**而本轮最值钱的不是这两个数**：上一轮那个"同工作点 = 空样本正确率 100%"
的判据，**分母只有 3** —— 纪律是对的，纪律锚定的指标当时太脆。
完整前沿、逐条对照、四项代价实测与统计精度限制见 **`docs/rag-vs-literal.md`**。

> ⚠️ **这两个数绑定在 2026-07-27 那张 64 节点图上，换图不复现。** 后续重跑
> 流水线出的图（07-28 的 66 节点、07-29 的 78 节点）标签都重挂过，字面基线在
> 同一工作点上分别是 **59%** 与 **65%**。**跨图比 recall 不成立** ——
> 分母、样本、标签本身都在变；要比就限定到两轮都存在的同一批观察句
> （见 `docs/pipeline-reproducibility.md` 末节）。向量没赢那个**结论**不受影响：
> 它是在同一张固定的图上只换打分器做出来的单变量对比。

接进 Claude Code：工作区根目录已带 `.mcp.json`，**重启 Claude Code** 后
`/mcp` 里应能看到 `cn-curriculum-graph` 与它的六个工具。

接真 LLM judge，激活 `NAME_DESC_MISMATCH`。两个 provider 可选，各认自己的 key：

```bash
export DEEPSEEK_API_KEY=sk-...                          # 默认 deepseek-v4-flash
uv run ccg-validate data/example-graph.json --judge deepseek

export ANTHROPIC_API_KEY=sk-ant-...                     # 默认 claude-haiku-4-5
uv run ccg-validate data/example-graph.json --judge anthropic

uv run ccg-validate data/example-graph.json --judge deepseek --model deepseek-v4-pro   # 升级档

# 先量 judge 判得准不准，再决定用哪个（对 ground truth 跑准确率/查准/查全）：
uv run python scripts/eval_judge.py --judge deepseek   # 16 条 ground truth，三档混淆矩阵
```

> ⚠️ **绝不要 `export ANTHROPIC_BASE_URL=https://api.deepseek.com/anthropic`**（DeepSeek 官方文档教的就是这招）。
> 同机的 Claude Code 自己也读这个变量，会被整个劫持到 DeepSeek 上。
> 本项目一律在代码里显式传 `base_url=`，key 用独立的 `DEEPSEEK_API_KEY`。

**生成流水线**：从课标原文跑出一份图（需 `DEEPSEEK_API_KEY`）。

```bash
# 素材放 data/source/*.md，段落以空行分隔，每段首行以条目编号开头（如 3.1.2）
uv run ccg-generate --source data/source --out data/generated
```

产出逐层落盘，可人眼检查（`01-chunks` → `02-drafts` → `03-deduped` → `04-edges`
→ `05-reviewed` → `graph.json`），外加跨层累加的 `dropped.json` 与 `review-log.json`。
**素材获取不属于流水线** —— 从 PDF 转文本也好、手敲也好，在管道外解决，
这条边界让法律风险不由这份代码承担（`docs/feasibility-analysis.md` 闸门 1）。

设计与实现依据见 `docs/pipeline-design.md`。

**换成 LangGraph 编排**（同一份六层纯函数，`--checkpoint` 支持断点续跑，
手写版没有这个能力）：

```bash
uv run ccg-generate --source data/source --out data/generated \
  --engine langgraph --checkpoint data/generated/.checkpoint.sqlite
# 再跑一次同样的命令即可从上次中断处续跑，不重跑已完成的层
```

**条目级扇出版**（B 阶段：抽取/审核真并发，checkpoint 粒度从"层"降到"条目"）：

```bash
uv run ccg-generate --source data/source --out data/generated \
  --engine langgraph-fanout --checkpoint data/generated/.checkpoint.sqlite
# --max-concurrency 默认 = min(32, cpu_count+4)，即本机 asyncio.to_thread
# 线程池上界；这是实测校准出来的拐点，见 docs/concurrency-calibration.md
```

两个编排实现的取舍见 `docs/langgraph-vs-handwritten.md`——**代码量多
1.65×~2.3×、msgpack 严格模式两个开关的组合陷阱、Node 级重试对多数真实
故障不可达**等代价都在里面如实列出，不只是"框架更省事"这一面。

跑受控实验复现这份笔记里的数字（fake 实现，非真实 API，见该脚本文档）：

```bash
uv run python scripts/compare_orchestration.py --chunks 10   # 三引擎对比：handwritten / langgraph / fanout
```

比两轮生成的图 —— 身份稳不稳、**误概念内容还在不在**（两个问题一次问完，
因为跑一轮是 ~1000 次调用 / 11 分钟；设计与局限见
`docs/pipeline-reproducibility.md` 末节）：

```bash
uv run python scripts/compare_runs.py 上一轮/graph.json 这一轮/graph.json
```

把校验层跑在 Marble 的真实数据上（验证规则在规模下有效）：

```bash
git clone --depth 1 https://github.com/withmarbleapp/os-taxonomy.git /tmp/marble
uv run python scripts/validate_marble.py /tmp/marble/data
```

## 架构

```
models.py            schema 本身（pydantic）——同时导出为 JSON Schema
validators/
  base.py            Finding / Severity
  structure.py       环、悬挂引用、孤立节点
  ordering.py        年级单调性、螺旋边递进性
  coverage.py        课标对齐率、provenance 完整性
  consistency.py     name/description 语义一致（judge 依赖注入）
judges/              Judge 协议的实现：(name, description) -> Verdict
  prompt.py          系统提示，所有实现共用（多样性要来自模型，不是提示词）
  anthropic_judge.py Anthropic 原生结构化输出 messages.parse(output_format=)
  deepseek_judge.py  DeepSeek 兼容端点，强制工具调用取结构化输出
pipeline/            生成流水线，六层各为 (input, deps) -> output 的纯函数
  models.py          内部类型；DraftContent 即给 LLM 的 input_schema
  chunk.py           纯规则切分，条目编号在这一层绑定
  extract.py         LLM 抽取候选知识点
  dedupe.py          规则配对 + LLM 确认 + 同名强制消歧
  edges.py           剪枝（由校验规则反推）+ LLM 连边
  review.py          三维度多判定器投票，分歧即淘汰
  assemble.py        组装成对外 schema，填 id / provenance
  run.py             编排 + ccg-generate 入口
runner.py            串起全部校验，ERROR → CI 红
cli.py               ccg-validate 入口
adapters/marble.py   仅用于把校验层跑在真实数据上，不引入其内容
```

### 校验规则一览

| 规则 | 严重级 | 存在的理由 |
|---|---|---|
| `CYCLE` | error | 先修边有环 = 学不动的死循环 |
| `DANGLING_REF` | error | 边指向不存在的节点 |
| `ISOLATED_TOPIC` | warning | 通常是抽取流水线漏接边 |
| `GRADE_INVERSION` | hard=error / soft=warning | 前置年级晚于后继 |
| `REVISIT_NOT_ADVANCING` | error | 螺旋边没指向更高年级 = 接反了 |
| `LOW_STANDARDS_COVERAGE` | error | 宣称"对齐课标"必须能被卡住 |
| `MISSING_PROVENANCE` | error | 无法判断某条数据可不可信 |
| `NAME_DESC_MISMATCH` | error | 名称与描述讲的是不同知识点，纯规则查不出 |
| `NAME_DESC_SCOPE_MISMATCH` | warning | 同一主题但名称罩不住描述的范围 —— 命名质量问题，不该让 CI 红 |
| `CONSISTENCY_SKIPPED` | warning | 显式声明哪项没跑，不静默略过 |

## 相对 Marble 的四处刻意差异

设计依据见 `docs/schema-design.md`，可行性论证见 `docs/feasibility-analysis.md`。

1. **`misconceptions` 字段** —— 典型误概念（孩子会怎么想错 + 诱发提问 + 纠正切入点）。
   Marble 完全没有。有了它，LLM 才能从"答错了"推进到"为什么这么想"。
2. **`revisits` 螺旋边** —— 建模"同一概念多轮次递进"（分数：三上→五下→六上）。
   中国课标是明确的螺旋结构，扁平 DAG 表达不了。
3. **每节点 `provenance`** —— 生成方式、审核状态、审核人、置信度。
   Marble 只有仓库级授权说明，不讲生产方法，这是它最大的信任缺口。
4. **双层对齐** —— `standards`（课标条目，codes-only）+ `textbook_units`（教材单元）。
   中国用户认"人教版四下第四单元"，不认"学段目标 2.3"。

另外用**年级**（1-9）而非年龄作为进度基元。

## 校验层已被真实数据验证

对 Marble 全量数据（1590 节点 / 3221 边）运行：

```
1598 error, 36 warning
✗ MISSING_PROVENANCE × 1590      （其数据无此字段）
✗ GRADE_INVERSION × 7            （hard 边年级倒挂）
✗ LOW_STANDARDS_COVERAGE × 1     （对齐率 51.4%，772/1590 未对齐）
! GRADE_INVERSION × 35           （soft 边）
! CONSISTENCY_SKIPPED × 1
```

对齐率这条**独立复现了此前手工核对的结果**（772 个未对齐节点）——
这是校验层确实在工作的证据，不是自说自话。

注：年龄→年级映射是有损的（超出 1-9 年级的一律夹逼），
所以按年龄算的 8 条 hard 倒挂在这里显示为 7 条。

## judge 实测（2026-07-26，deepseek-v4-flash）

### 判定标准为什么是三档

初版是 `consistent: bool`，对 8 条手写 ground truth 拿了 100%。但抽 124 个 Marble
真实节点一跑，11 条判为不符里有 6 条既不是"讲同一件事"也不是"讲不同知识点" ——
名称与描述**同属一个主题、覆盖范围对不上**（`Deep-Sea Survival` 的描述扩到了
木蛙和水熊虫）。手写样本全是"乘法 vs 除法"这种泾渭分明的，压根没覆盖这一类。

一起判 ERROR 会让 CI 被"名字起窄了"的节点刷红，静默放过又漏掉真实的命名问题，
所以拆成 `topic_mismatch`(ERROR) / `scope_mismatch`(WARNING) / `consistent` 三档，
ground truth 扩到 16 条（含那 6 个边界案例 + 1 条初版误判的对抗样本）。

改完对 16 条 ground truth：**每一档查准查全都是 100%**。

### 全量 1590 节点

10 并发，205 秒，**0 次调用失败**，约 $0.08：

```
consistent        1454   91.4%
scope_mismatch     108    6.8%   → WARNING
topic_mismatch      28    1.8%   → ERROR
```

28 条 ERROR 抽看全是真问题：`Number bonds to 9` 的描述在讲凑十、`Tenths` 的描述
在讲百分位、`Types of angles` 的描述在讲勾股定理、`Measuring length` 的描述在讲体积。

**而且 86%（24/28）的 ERROR 落在跨年龄段复用的同名节点上** —— `Understanding angles`
一名七用，其中 4 个的描述分别在讲长方形面积、四边形分类、尺规作图、长方形性质。
这不是随机噪声，是他们生成流水线的结构性缺陷：名称被当成"主题族标签"复用，
而描述来自各年龄段的具体课标条目，两者逐级漂移。**同名节点是这类数据集的高危区**，
本项目的生成流水线要专门防这一手。

注：`mt_H6LlpWgEYS` 被判为一致是**正确**的 —— 它的问题在 description 与
assessmentPrompt 之间，不在 name 与 description 之间，因此刻意没进 ground truth。

## 生成流水线首次真实运行（2026-07-26，deepseek-v4-flash）

3 条课标条目 → 94 秒 → 4 节点 1 边，0 error / 3 warning。产出率不高，但**每一条
没能进去的都有账可查**（`dropped.json` 13 条）。真实数据一跑就暴露了单测覆盖不到的东西：

**① 忠实度判定器第一次跑就抓到了真问题。** `小数的意义` 这个节点被 fidelity 判否：
原文只有「能理解小数的意义」，而模型的描述写成了「小数是**十进制分数**的另一种表示
形式，知道各数位的**位值**含义」—— 这两个概念原文里没有，是模型自己加的。这正是
这一层存在的理由。

**② 但淘汰会制造孤儿，流水线对此毫无感知。** `小数的意义` 是最基础的那个节点，
它被淘汰后，`小数大小的比较`、`简单的小数加减运算` 全成了孤儿（两条 `ISOLATED_TOPIC`）。
**没有任何机制说「这次淘汰孤立了 N 个后继」** —— 这是跨层的语义缺口，不是某一层的 bug。

**③ 剪枝没反推 `CYCLE`，真实数据当场证实。** 模型对两个**同年级**节点提出了双向边
（`万以内数的认识与读写` ↔ `用数描述事物的多少`）。剪枝规则只反推了 `GRADE_INVERSION`
（前置年级不得晚于后继），而同年级互为候选是允许的 —— 这次是 edge judge 把两条都否了
才没成环，**靠判定器兜住而不是靠剪枝挡住**。

**④ 留痕机制反过来误导了人。** 末尾报告打印 `CONSISTENCY_SKIPPED`（未提供 judge，
已跳过名实一致校验），但 review 层**明明跑过** name judge —— 是最后那次 `run_all`
不知情。这条恰恰是本项目最在意的机制自己出的岔子。

**⑤ 一条真实抽取失败**：`分数` 那条产出零草稿（`NO_DRAFTS`），源文本明明有内容。
好在没有静默。

## 学到什么

- **规则能查的和查不了的，要分开设计。** 结构层（环、悬挂、单调性）纯规则即可，
  且 Marble 这一层做得扎实（0 环 0 悬挂 0 孤立）；真正出问题的是内容层
  （name 与 description 讲的不是一回事），只能靠语义判断 → 把 judge 做成
  依赖注入，CI 接真 LLM，测试接确定性假判定器。
- **结构化输出不是一个可移植的特性，"强制工具调用"才是。** DeepSeek 的 Anthropic
  兼容端点照收 `output_format` 却不遵守（实测返回自由文本"否"，SDK 在 JSON 解析上炸）；
  改用「强制调用一个 `input_schema` 就是 `Verdict` 的工具」后即通。任何支持 tool use
  的模型都能跑这条路，代价是要自己 `model_validate` 一遍。
- **judge 多样性要来自模型，不能来自提示词。** 两个 judge 共用 `judges/prompt.py`
  同一份系统提示 —— 否则分歧分不清是"模型看法不同"还是"问题问得不一样"，
  将来的投票机制就没意义了。
- **判定的档位数是产品决策，不是工程细节。** 二值 judge 在干净样本上 100%，
  一上真实数据就暴露出第三类情形。档位不够时模型不会告诉你"没有合适的选项"，
  它会硬塞进现有的某一档 —— 于是 ERROR 里混进一堆本该是 WARNING 的东西。
  **ground truth 的样本分布决定了你能发现什么**：全是极端案例就测不出边界在哪。
- **"跳过"必须留痕。** 不传 judge 时产出 `CONSISTENCY_SKIPPED` 警告而非静默通过 ——
  否则"CI 绿了"会被读成"全都查过了"，这正是 Marble 让人误判其数据质量的方式。
- **原则会在接缝处被稀释。** 六层各自都老老实实记了 `DropRecord`，`review.py` 甚至为
  丢边写了完整的 `UNKNOWN_REVIEW_TARGET` 记账 —— 但编排层一行 dict comprehension
  的预过滤让那段代码**永不执行**，真正丢边的地方一声不吭。**记账代码写在了走不到的
  分支上。** 逐层审查看不见这个，因为它不在任何一层里。修法之外更重要的是：
  把原则写成**跨层守恒断言**（最终产出 + 全部 DropRecord ≡ 全部输入），
  让"没有静默跳过"从口号变成会失败的测试。
- **fake 喂完美输入，是测试套件最大的系统性盲区。** 159 个测试里，每层的 fake 都给
  自己喂本层的理想输入：extract 的 fake 从不返回倒挂年级，edges 的 fake 从不返回重复边，
  端到端的 fake 让每个 judge 都点头。**没有一个测试跑过"部分成功 + 部分失败"** ——
  而那才是真实运行的常态。四个 Critical 全部落在这个盲区里。
- **跑真实数据是最好的测试。** 单测全绿之后跑 Marble，当场暴露两个 bug：
  报告按 code 分组导致 error 被藏进 warning 组；适配器漏映射 standards
  导致对齐率虚报成 0%。两个都是先补失败测试再修的。

## 下一步

1. ✅ ~~接真 LLM judge，把 `NAME_DESC_MISMATCH` 跑起来~~ ——
   `judges/anthropic_judge.py`（原生结构化输出）+ `judges/deepseek_judge.py`（强制工具调用）
2. ✅ ~~用真 key 量 judge 准不准~~ —— deepseek-v4-flash 对 ground truth 8/8，
   Marble 抽样 124 个 0 失败
3. ✅ ~~定死"范围不匹配算不算名实不符"~~ —— 拆成三档判定 / 两级严重性，
   ground truth 扩到 16 条，全量 1590 节点跑出 28 ERROR + 108 WARNING
4. ✅ ~~生成流水线骨架~~ —— 六层纯 DAG 工作流，159 测试，已接真模型端到端跑通
5. **接真模型上量前必须先修**（首次真实运行暴露，见上一节）：
   - 剪枝反推 `CYCLE`：同年级互为候选会产出双向边，目前靠 judge 兜住而非剪枝挡住
   - 淘汰制造孤儿无感知：基础节点被淘汰后，后继静默失去前置
   - `run_all` 不知道 review 层已跑过 name judge，误报 `CONSISTENCY_SKIPPED`
   - 补重试/退避与 `--from <stage>` 重入（设计文档已写、尚未实现；
     没有重试则瞬时错误持续侵蚀产出率，没有重入则任何中断都要从头烧钱）
   - `candidate_pairs` 是 O(n²) 且每对跑 `SequenceMatcher`，上量会撞墙
6. 配 `ANTHROPIC_API_KEY`，把交叉审核换成跨训练谱系双票 ——
   现在是同族（flash + pro），误判高度相关，投两次约等于投一次
7. ✅ ~~接真模型上量前的三件事~~（2026-07-27，三条全部完成且都推翻了原判断）：
   - **重评 `ValueError` 不重试的误伤面** → `docs/error-taxonomy.md`。
     误伤面今天确实是零，但零是"恰好"来的（靠 catch 边界的当前形状）。
     修法不是收窄排除集合（那会把 `JSONDecodeError`/`UnicodeDecodeError`/
     `ValidationError` 这些真·确定性错误变成可重试的），而是把唯一一个
     语义上不属于这类的异常搬出 `ValueError`：新增
     `errors.ToolCallMissingError`。改完之后"`ValueError` = 不重试"
     从碰巧成立变成按构造成立。
   - **并发上限按实际配额校准** → `docs/concurrency-calibration.md`。
     实测发现 **provider 根本不是瓶颈**：DeepSeek 给的是账户级并发连接数
     2500(flash)/500(pro)，全档爬坡 0 个 429，原默认值 8 低了两个数量级。
     真正的天花板是 `asyncio.to_thread` 的默认线程池 `min(32, cpu+4)`
     ——那是当初为让 `NODE_TIMEOUT` 生效而引入的，两者是同一决定的两面。
     吞吐拐点正好压在这道墙上（19 → 12.60 req/s，24 → 12.09 且 p95 +47%），
     默认值改为跟着这道墙走的公式。顺带补上 `--engine langgraph-fanout`
     与 `--max-concurrency`（此前扇出版没有任何生产入口）。
   - **注册 `allowed_msgpack_modules`** → `tests/pipeline/test_checkpoint_serde.py`。
     原判断"未注册会让 checkpoint 整个失效"两处不成立：框架 compile 时会
     自己从 state schema 推导 allowlist；未注册的后果也不是失效，而是
     **静默把 pydantic 模型降级成 dict**（症状是下游一句
     `AttributeError: 'dict' object has no attribute ...`，看起来像业务
     bug）。真正的坑是"serde 严格"与"自动推导"是**两个独立开关**，而
     `严格 + 未设环境变量` 这一格比什么都不做更糟——已实测踩中并修复。
8. ✅ ~~MCP server，把图暴露给 agent~~（2026-07-27，含真实对话验收）：
   `serve/query.py`（六个工具的纯函数领域层，不 import 任何 mcp 符号）+
   `serve/mcp_server.py`（FastMCP 1.28.1 绑定，函数体只转发）+
   `scripts/eval_diagnosis.py`（**30 条** ground truth，recall@3 = 84% @ 生产阈值 0.2；
   **同工作点（空样本正确率 100%）上的基线是 79% @ 阈值 0.25**，见第 12 条）。
   实测发现见 `docs/mcp-server-design.md` §7，三条与设计预期不同：
   首跑 63% 掉的不是语义理解而是两个实现缺陷（长度惩罚 / 符号与口语的表示层差异）、
   IDF 加权实测更差、以及"选 @3 不选 @1"的理由在当前实现下不成立
   （@1/@3/@5 三个数完全相同，排序维度是空的）。
   **验收已跑**（两段真实对话，逐字段核对见 `docs/mcp-server-design.md` §8）：
   `misconceptions` 兑现了承诺 —— 诊断线上靠「0.3 比 0.03 小」这条**同型不同数**的
   误概念定位到心智模型，那正是当初决定不放 LLM 时最担心的能力，结果由字段本身补上了
   （误概念是有限且可枚举的，不需要语义模型也能对上）。`evidence` / `assessment_prompt` /
   `dependencies.reason` 均原样进入回答，`provenance` 让 agent 自动降级措辞。
   **`revisits` 完全没用上**（全图 0 条，字段至今未经检验）；`standards` 只报编号，
   对家长无意义 —— 那个字段的消费者是教研/教师，不是家长。
   **规划线不可用**：分数四则的前置只有一条，通分整条链没连出来，agent 只能用自身知识
   补三步并标注来源。是数据问题不是工具问题 —— 而 `get_graph_stats` 先报 26/64 孤立，
   让 agent 一开始就知道该打几折，那个"防止在退化的图上假装做规划"的设计意图起了作用。
9. ✅ ~~路线图"阶段四·主流框架"：LangGraph 编排对比~~ —— 同一份六层纯函数
   流水线接出手写版 / LangGraph 版 / LangGraph `Send` 扇出版三种编排实现，
   受控实验量出断点续跑的真实收益与代价，见 `docs/langgraph-vs-handwritten.md`
   （核心结论：省的是崩溃点上游的调用量，但代价是 1.65×~2.3× 代码量、
   Node 级重试对多数真实故障不可达、msgpack 严格模式的开关陷阱、异步传染）
10. **边质量 —— 现在的主要瓶颈，规划线就卡在这里**：31 条边、26/64 孤立、
   最长前置链 3 层，跨学段前置（整数→小数→分数→比和比例）没连出来，
   `分数的意义` 至今是孤儿。涉及 edges 层剪枝策略，是独立课题。
11. 补一个"谁以它为前置"的工具（`get_next_topics`）——"他会了 X，接下来学什么"
   是家长的自然问题，现有六个工具答不了；领域层已有 `dependents_of`，只是没暴露。
   **这条是真跑对话才发现的，设计评审发现不了。** 排在第 10 条之后：
   同一张稀图上，新工具照样大面积返回空。
12. ✅ ~~路线图"阶段三·RAG"入口：字面匹配 vs 向量检索的受控对比~~
   （2026-07-27 首轮，**2026-07-28 补空样本后重扫**，见 `docs/rag-vs-literal.md`）
   —— **向量没有赢**。同工作点（空样本正确率 = 100%）上字面 **79%**（阈值 0.25）
   vs bge-m3 向量 **53%**（阈值 0.75），差 26pp = 19 条里差 5 条。
   代价实测：模型 4.3G、依赖 +744M、建索引 0→3.2s（热启动）/~10.5s（上一轮口径，
   含更多首次开销）、单次查询 1.6ms→12~30ms（**延迟这一项是实现绑定的**，
   大头是纯 Python 余弦循环，向量化后可到亚毫秒级；不可压缩的是前三项，
   见 `docs/rag-vs-literal.md` §6.4）。
   四条附带结论：**(a)** ⭐ **本轮最贵的一条 —— 判据本身太脆**：上一轮"同工作点
   = 空样本正确率 100%"这个保证公平性的判据，**分母只有 3**，而那 3 条恰好都是
   "完全不相关"的容易样本。补到 11 条后每个基线都变了（生产阈值 0.2 从 100% 掉到
   91%、字面工作点 0.15→0.25、基线 89%→79%、向量 0.7→0.75、79%→53%）。
   **纪律守住了"不许作弊"，守不住"指标本身没意义"** —— 这是"小样本上的百分比"
   第一次不是发生在被比较的对象身上，而是发生在**裁判身上**（§6.0）；
   **(b)** "字面基线"被层层更正过两次：84%（阈值 0.2）→ 89%（0.15）→ **79%（0.25）**，
   每次变的都是判据不是模型 —— 引用基线必须连工作点一起写；
   **(c)** 立项时点名"必须上向量才能救"的三条样本，向量一条也没救回来
   —— **立项理由本身没站住**；
   **(d)** 26 个百分点在 n=19 上只等于 5 条样本（±1 条 ≈ ±5.3pp），
   **差距变大不等于证据变强**；本轮拿不到向量的逐条明细，做不了配对检验。
   能说的仍然是"向量没赢 + 代价确定"，**不能**说"字面显著更优"。
   顺带：生产默认值 0.2 现在有一条已知误招（`empty_bar_chart`，相关度 0.2105），
   **0.2 vs 0.25 是一个开放问题**，见 `docs/rag-vs-literal-backlog.md`。
   换模型是一行；**hybrid 不是** —— 排名融合需要打分器看到候选集，现协议
   （成对打分）给不了，那一步要先扩协议（或换成 `Retriever` 形状），详见
   `docs/rag-vs-literal.md` §6.6/§6.7。

> 法律定性（课标著作权，`docs/feasibility-analysis.md`）只卡"大规模生成 + 对外发布"；
> 本地自用的流水线跑通不受影响。

## 许可与来源

- 本项目代码：随工作区
- `adapters/marble.py` 仅读取 Marble 数据用于校验层验证，不再分发其内容。
  Marble Skill Taxonomy 许可：ODbL 1.0（数据库）+ CC BY-SA 4.0（内容）
