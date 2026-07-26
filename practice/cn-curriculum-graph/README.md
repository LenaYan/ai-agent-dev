# cn-curriculum-graph —— 中国课标知识依赖图（schema + CI 校验层）

## 这是什么

把中国义务教育课标拆成可教、可评的 micro-topic，用先修依赖边连成图，
让 LLM 能在其上做定位与调度 —— 对标 [Marble Skill Taxonomy](https://github.com/withmarbleapp/os-taxonomy)，
但换成中国课标，并修掉原版几个已确认的缺陷。

**当前进度：只做了地基。** schema 定义 + CI 校验层已完成并测试通过；
**图数据本身一个节点都还没有**。这是刻意的顺序 —— 先有校验，才谈生成。

> ⚠️ 这个项目的**真正资产是流水线，不是数据集**。
> 数据集的可信度取决于教研专业审核（见 `docs/feasibility-analysis.md` 第二节），
> 那一环靠工程补不上；而"从非结构化文本生成并自校验结构化知识图"
> 这条链是可复用的能力，换个领域（法规、内部文档、合规要求）立刻能用。

## 怎么跑

```bash
uv sync
uv run pytest                        # 58 个测试
uv run ccg-validate data/example-graph.json  # 校验一份图数据（默认跳过语义一致性，留 CONSISTENCY_SKIPPED 警告）

uv run python scripts/export_schema.py   # 重新导出 JSON Schema
```

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
4. 生成流水线（多 agent 抽取 + 交叉审核），产出第一批「数与代数」节点 ——
   其"交叉审核"层复用这套 judge + ground truth 基建，且 Anthropic/DeepSeek
   两个不同训练谱系的 judge 正好当独立投票者（同族模型误判高度相关，投票会失效）
5. MCP server，把图暴露给 agent

> 法律定性（课标著作权，`docs/feasibility-analysis.md`）只卡"大规模生成 + 对外发布"；
> 本地自用的流水线跑通不受影响。

## 许可与来源

- 本项目代码：随工作区
- `adapters/marble.py` 仅读取 Marble 数据用于校验层验证，不再分发其内容。
  Marble Skill Taxonomy 许可：ODbL 1.0（数据库）+ CC BY-SA 4.0（内容）
