# 生成流水线设计（第一轮）

> 定稿 2026-07-26。配套：`schema-design.md`（产出物的形状）、
> `feasibility-analysis.md`（为什么这么划边界）。

## 1. 目标与非目标

**目标**：从一段课标原文，跑出一份能通过 `ccg-validate` 且 0 error 的 `graph.json`。
每一层都有可单独重跑、可人眼检查的中间产物。

**明确不承诺**：内容的教研专业正确性。依赖边的正确性没有形式化验证方法
（`feasibility-analysis.md` 闸门 2），这一环靠工程补不上。所以本轮衡量的是
**管道是否可观测、可重入、不静默吞错**，不是产出是否可信。

**本轮不做**（YAGNI）：

| 不做 | 为什么 |
|---|---|
| `revisits` 螺旋边 | 需要跨学段素材，单节输入喂不出来 |
| `textbook_units` | 需教材目录，属另一素材源，且教材版权风险远高于课标 |
| 审核不通过后的反馈重生成 | "重生成会变好"这个前提目前没有证据，见 §8 |
| 并发、缓存、增量 | 几十个节点串行足够；1590 节点 10 并发也才 205 秒 |

## 2. 输入契约

流水线的输入是 `data/source/*.md` 里的纯文本。**素材获取不属于流水线**——
从 PDF 转出来也好、手敲也好，都由使用者在管道外解决。

这条边界是刻意的：课标条目的著作权定性尚无明确判例
（`feasibility-analysis.md` 闸门 1），把获取环节挡在代码之外，
法律风险就不由这份代码承担。schema 的 `codes-only` 策略同理——
`Standard` 只存 `curriculum` + `code`，不存条目原文。

## 3. 分层

六步，其中三步不需要模型。这个比例本身是设计质量的信号
（`effective-agents-principles.md` 心法①：不要什么都做成 Agent）。

```
data/source/*.md
  ↓ chunk      纯规则          01-chunks.json
  ↓ extract    LLM，每 chunk 一次   02-drafts.json
  ↓ dedupe     规则配对 + LLM 确认  03-deduped.json  merges.json
  ↓ edges      LLM，每节点一次      04-edges.json
  ↓ review     多 judge 投票       05-reviewed.json  review-log.json
  ↓ assemble   纯规则             graph.json
  ↓ run_all                     校验报告，ERROR → 退出 1

dropped.json 是跨层的：任何一层丢弃的条目都追加写入同一份文件。
```

每层签名统一为 `(input, deps) -> output` 的纯函数，`deps` 里的 LLM 客户端
依赖注入 —— 与 `judges/` 现有做法一致，目的是每层都能用 fake 单测、零网络零 key。

`--from <stage>` 可从任意层重入，读上一层的落盘文件。

### 3.1 chunk（纯规则）

按课标条目切分文本，产出带稳定标识的片段。

```python
class Chunk(BaseModel):
    id: str            # f"{source_stem}#{ordinal:03d}"，确定性
    text: str
    standard_code: str # 该条目的课标编号，如 "3.1.2"
    source_file: str
    ordinal: int
```

**编号必须在 chunk 层绑定，不能留给抽取层。** 原因是校验层的
`LOW_STANDARDS_COVERAGE` 是 ERROR 且带硬阈值：抽不到编号的节点会拉低对齐率，
足以让整批产出被自己的 CI 拒掉。而编号在原文里是有格式的，属于纯规则能干的活，
交给模型只会平白引入不确定性。

切不出编号的片段直接进 `dropped.json`（原因码 `NO_STANDARD_CODE`）——
这通常意味着切分规则与该份素材的排版不匹配，是需要人看的信号，不该带病往下走。

### 3.2 extract（LLM）

每个 chunk 一次调用，产出 0..n 个 `TopicDraft`。结构化输出走**强制工具调用**
（`input_schema` = `list[TopicDraft]` 的包装对象），理由见 README「学到什么」：
原生 `output_format` 在兼容端点上会静默降级，强制工具调用才可移植。

失败（API 错、schema 不合法、重试 3 次仍失败）→ 该 chunk 进 `dropped.json`，
不中断整批。

### 3.3 dedupe（规则配对 + LLM 确认）

同一知识点常被多条课标条目提到。

1. **规则配对**（只负责缩小范围，宁可多给候选）。满足任一即进候选对：
   - 归一化名称相同 —— 归一化 = 去空白、去标点、转小写、全角转半角
   - 归一化名称的 `difflib.SequenceMatcher` 比值 ≥ 0.85
   - `standard_codes` 有交集
2. **LLM 确认**：`(草稿A, 草稿B) -> 是否同一知识点`。这与现有 judge 的
   `(name, description) -> 关系` 同构，复用 `judges/` 骨架扩一个实现即可。
3. **合并**：判为同一的，选一份作基底 —— 先比 `evidence` 条数，多者胜；
   并列则取 `description` 更长的；再并列取 `id` 字典序小的（保证确定性）。
   `evidence` 与 `standard_codes` 取并集去重，`misconceptions` 按 `statement`
   去重后合并。合并记录写 `merges.json`。

**同名不同义强制处理**：若两个草稿名称相同但判为不同知识点，必须显式解决 ——
要么给名称加限定词，要么合并。不允许同名不同义共存。

这条规则是从实测数据里挣来的：对 Marble 全量 1590 节点跑名实一致判定，
28 条 ERROR 里 **86%（24/28）落在跨年龄段复用的同名节点**上
（`Understanding angles` 一名七用，其中 4 个的描述分别在讲长方形面积、
四边形分类、尺规作图、长方形性质）。那不是随机噪声，是"名称当主题族标签复用、
描述来自各学段具体条目"导致的逐级漂移。**同名节点是这类数据集的高危区。**

### 3.4 edges（LLM）

朴素做法是两两配对，30 个节点就是 435 次调用。改为**剪枝 + 每节点一次**：

**剪枝规则直接由校验规则反推**：

- `GRADE_INVERSION` 会拒绝"前置年级晚于后继" → 只在 `grade_start(前置) ≤ grade_start(后继)`
  的有序对里找候选
- 跨度超过 2 个年级的先修基本是间接的 → 砍掉，让它通过传递性表达

然后把「该节点 + 它的全部候选前置」一次性给模型，让它输出选中的边和 `reason`。
调用次数从 N² 降到 N。

省钱是次要的。**主要价值是生成端不会产出校验端注定要拒的东西** ——
同一套约束写两遍是 bug 的温床。

### 3.5 review（多 judge 投票）

审三样，都是已知会出问题的：

| 审什么 | 判据 |
|---|---|
| 抽取忠实度 | `description` 是否真出自 `source_span`，还是模型自己发挥 |
| 名实一致 | 复用现有三档 judge（consistent / scope_mismatch / topic_mismatch） |
| 边的合理性 | `reason` 站不站得住 |

**投票策略：分歧即淘汰。** 本轮定位是不承诺内容正确，那就宁可少产出也别放可疑的
进去。被淘汰的那批写进 `dropped.json`，它是最值得人工复核的清单 ——
比随机抽检有价值。

**投票者组成（现实妥协，已知短板）**：理想是 Anthropic + DeepSeek 两个不同训练
谱系互投——同族模型的误判高度相关，投两次约等于投一次。但当前环境只有
`DEEPSEEK_API_KEY`。故默认配置为 `deepseek-v4-flash + deepseek-v4-pro`：
**同族双票，独立性打折**。投票者做成可配置列表，配上 Anthropic key 后
改参数即为真正的跨谱系投票。这一点必须写进产出物的说明，不能让人误读成
"两个模型都同意所以可信"。

### 3.6 assemble（纯规则）

补齐代码负责的字段（见 §5），组装成 `CurriculumGraph`，丢弃流水线内部字段
（`source_span`），落 `graph.json`，然后跑 `run_all`。ERROR 即失败退出 1。

## 4. 数据模型

```python
class TopicDraft(BaseModel):
    """抽取产出物。刻意不是 Topic —— 见 §5 的字段归属。"""
    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    type: TopicType
    subject: str
    domain: str
    grade_start: int
    grade_end: int
    evidence: list[str] = Field(min_length=1)
    assessment_prompt: str
    misconceptions: list[Misconception] = Field(default_factory=list)
    source_span: str   # 抽自原文哪一句，供审核层复核；assemble 时丢弃

    # 以下由流水线填，不在给模型的 input_schema 里
    chunk_id: str
    standard_codes: list[str] = Field(default_factory=list)


class Vote(BaseModel):
    reviewer: str      # 模型 id
    approved: bool
    reason: str


class ReviewOutcome(BaseModel):
    target: str        # draft id 或 edge key
    aspect: str        # fidelity / name_desc / edge_reason
    votes: list[Vote]
    approved: bool     # 全票通过才 True


class DropRecord(BaseModel):
    stage: str         # chunk / extract / dedupe / edges / review
    ref: str           # chunk id / draft id / edge key
    reason: str        # 机器可读的短码
    detail: str = ""
```

`standard_codes` 用 `list[str]` 而不是 `list[Standard]`：`Standard.curriculum`
是常量，assemble 时补上即可。

## 5. 字段归属：模型只产出它有资格产出的字段

| 字段 | 谁填 | 理由 |
|---|---|---|
| `name` `description` `evidence` `assessment_prompt` `misconceptions` | 模型 | 内容判断 |
| `type` `subject` `domain` `grade_start` `grade_end` | 模型 | 内容判断 |
| `source_span` | 模型 | 溯源锚点 |
| `standard_codes` | 代码 | 由 chunk 携带（§3.1），不经模型 |
| `chunk_id` | 代码 | 溯源 |
| `standards[].curriculum` | 代码 | 常量 |
| `id` | 代码 | 见下 |
| `provenance` | 代码 | 见下 |

给模型的 `input_schema` 只含"模型"那几行 —— 少一个字段就少一个出错面。

**`id` 为什么不让模型编**：模型生成的 id 在去重合并后必然撞车。改用确定性哈希
`sha1(name + "|" + domain + "|" + grade_start)` 前 10 位，前缀 `t_`。
它在重排输入时保持稳定；若发生碰撞，说明这三项全同 —— 那本就该在 dedupe
阶段被合并，此时直接报错而非静默覆盖。

**`provenance` 为什么不让模型填**：自己声明自己可信是没有意义的。这正是
Marble 最大的信任缺口（`feasibility-analysis.md`），本项目的立身之本就是修掉它。
由代码写死：

- `method` = `llm-extract/<模型 id>`
- `review_status` = `unreviewed`（恒定，直到有人真的审过）
- `confidence` = **`0.0`**

`confidence` 留 0.0 是刻意的。它的语义是"这条数据可不可信"，在没有任何教研审核
之前，任何非零值都是自欺。投票结果不换算成 confidence，而是单独记进
`review-log.json` —— 模型间的一致程度和教研正确性是两件事，混进一个数字里
就再也分不开了。

## 6. 错误处理

- 单条目失败不中断整批：写 `DropRecord` 进 `dropped.json`，继续跑
- API 调用重试 3 次，指数退避
- 结构化输出校验失败按调用失败处理（强制工具调用只保证"调了工具"，
  不保证参数合法，仍要 `model_validate` 一遍）
- 每层落盘后才进入下一层，任意层可用 `--from` 重入
- **没有静默跳过**：每一条没进最终产物的东西，都必须在 `dropped.json` 里
  留下带原因的记录。这与校验层的 `CONSISTENCY_SKIPPED` 是同一个原则 ——
  "跳过"必须留痕，否则"跑完了"会被读成"全都成了"

## 7. 测试策略

- 每层注入 fake LLM 做单测，沿用 `judges/` 的依赖注入模式：零网络、零 key
- `chunk` / `assemble` 是纯规则，直接测
- 端到端：全 fake 跑一遍完整管道，断言产出能过 `run_all` 且 0 error
- 真模型的验证靠手动跑一次 + 人眼看中间产物；本轮不为真模型输出写断言
  （内容正确性不在承诺范围内，为它写断言等于假装能验证）
- 全程 TDD：先写失败测试，看到 RED，再实现

## 8. 已知风险与未决

| 项 | 状态 |
|---|---|
| 同族双票的独立性折扣 | 已知短板，配 Anthropic key 后解决（§3.5） |
| 依赖边正确性无形式化验证 | 结构性问题，工程补不上（`feasibility-analysis.md` 闸门 2） |
| LLM 输出可能与教材实质性相似 | 自动化解决不了，靠人工改写兜底（同上，闸门 1） |
| 反馈重生成是否有效 | 未验证。要先证明"带着批评重写"确实优于"重试"，否则那个环只是无限循环的体面说法。等 DAG 版跑出真实失败模式再评估 |
| 课标条目著作权定性 | 无明确判例。本地自用不受影响；大规模生成 + 对外发布前需书面法律意见 |

## 9. 与 effective-agents 心法的对应

- **①不要什么都做成 Agent**：六步里三步是纯规则，无一步需要自主决策。
  这是工作流不是 agent，控制流全在代码里。
- **②尽可能久地保持简单**：不引框架、不加反馈环、不做并发缓存。
  将来用 LangGraph 重写一遍做对比笔记，正是 `roadmap.md` 阶段四的产出要求 ——
  但要先有手写版跑出的真实失败模式，否则学到的只有 API。
- **③像你的 Agent 一样思考**：每层落盘可人眼检查，`dropped.json` 记清每条
  为什么没能进来。看不见中间状态就没法判断它到底在干什么。
