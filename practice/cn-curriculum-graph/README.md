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
uv run pytest                        # 44 个测试
uv run ccg-validate data/graph.json  # 校验一份图数据（默认跳过语义一致性，留 CONSISTENCY_SKIPPED 警告）

uv run python scripts/export_schema.py   # 重新导出 JSON Schema
```

接真 LLM judge，激活 `NAME_DESC_MISMATCH`（需 `ANTHROPIC_API_KEY`）：

```bash
export ANTHROPIC_API_KEY=sk-...                          # 或写进工作区根目录 .env
uv run ccg-validate data/graph.json --judge anthropic    # 默认 Haiku 4.5
uv run ccg-validate data/graph.json --judge anthropic --model claude-sonnet-5

# 先量 judge 判得准不准，再决定用哪个模型（对 ground truth 跑准确率/查准/查全）：
uv run python scripts/eval_judge.py
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
| `NAME_DESC_MISMATCH` | error | 名实不符，纯规则查不出 |
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

## 学到什么

- **规则能查的和查不了的，要分开设计。** 结构层（环、悬挂、单调性）纯规则即可，
  且 Marble 这一层做得扎实（0 环 0 悬挂 0 孤立）；真正出问题的是内容层
  （name 与 description 讲的不是一回事），只能靠语义判断 → 把 judge 做成
  依赖注入，CI 接真 LLM，测试接确定性假判定器。
- **"跳过"必须留痕。** 不传 judge 时产出 `CONSISTENCY_SKIPPED` 警告而非静默通过 ——
  否则"CI 绿了"会被读成"全都查过了"，这正是 Marble 让人误判其数据质量的方式。
- **跑真实数据是最好的测试。** 单测全绿之后跑 Marble，当场暴露两个 bug：
  报告按 code 分组导致 error 被藏进 warning 组；适配器漏映射 standards
  导致对齐率虚报成 0%。两个都是先补失败测试再修的。

## 下一步

1. ✅ ~~接真 LLM judge，把 `NAME_DESC_MISMATCH` 跑起来~~ ——
   `judges/anthropic_judge.py` + `--judge anthropic` + `scripts/eval_judge.py`（评测 ground truth）
2. 用真 key 跑 `eval_judge.py`，量 Haiku 4.5 判得准不准；不够再升 Sonnet
3. 生成流水线（多 agent 抽取 + 交叉审核），产出第一批「数与代数」节点 ——
   其"交叉审核"层复用这套 judge + ground truth 基建
4. MCP server，把图暴露给 agent

> 法律定性（课标著作权，`docs/feasibility-analysis.md`）只卡"大规模生成 + 对外发布"；
> 本地自用的流水线跑通不受影响。

## 许可与来源

- 本项目代码：随工作区
- `adapters/marble.py` 仅读取 Marble 数据用于校验层验证，不再分发其内容。
  Marble Skill Taxonomy 许可：ODbL 1.0（数据库）+ CC BY-SA 4.0（内容）
