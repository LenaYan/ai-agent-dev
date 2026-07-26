# LangGraph 重写编排层 + 手写 vs 框架对比实验（设计）

> 定稿 2026-07-26。配套：`pipeline-design.md`（手写版设计）、
> `../../docs/roadmap.md` 阶段四（本文是其"同一需求分别用手写和框架实现，
> 写对比笔记"的验收物）。

## 1. 目标与非目标

**目标**：用 LangGraph 重写生成流水线的**编排层**（六层纯函数一行不改），
两个实现跑同一份素材、注入同样的故障，**用数据**回答两个问题：

1. 同样的编排，框架省了什么、又强加了什么？
2. 框架让我做成了原本不会去做的事吗？收益多大？

**非目标**：
- 不追求 LangGraph 版在生产上替代手写版 —— 两者长期共存，共存本身就是笔记的前提
- 不做 LangGraph Platform / Studio / 异步 / Postgres checkpointer
- 不接真模型上量（受控实验用 fake，理由见 §5）

**为什么现在做**：手写版已跑通并**带着真实的失败模式**（judge 抛 429 会炸全批、
中断只能从头烧钱）。空着手写状态机，学到的只有 API；带着具体痛点写，
才知道 checkpointer 到底解决了什么、代价是什么。

## 2. 已核实的 LangGraph API（2026-07-26 实测）

版本：`langgraph==1.2.9`、`langgraph-checkpoint==4.1.1`、
`langgraph-checkpoint-sqlite==3.1.0`、`langchain-core==1.5.1`。

> ⚠️ 本节是**实测内省结果**（装进临时环境跑 `inspect.signature`），不是文档转述。
> LangGraph 1.2 于 2026-05 发布，变化快，后续动手前应重新核实。

```python
# 节点签名：(state, runtime)，用 runtime.context 取运行时依赖
def node(state: PipelineState, runtime) -> dict: ...

StateGraph(state_schema, context_schema=None, *, input_schema=None, output_schema=None)

add_node(name, action, *, retry_policy=None, error_handler=None, timeout=None,
         cache_policy=None, defer=False, destinations=None)

RetryPolicy(initial_interval=0.5, backoff_factor=2.0, max_interval=128.0,
            max_attempts=3, jitter=True, retry_on=default_retry_on)   # NamedTuple

compile(checkpointer=None, *, cache=None, store=None,
        interrupt_before=None, interrupt_after=None)

invoke(input, config={"configurable": {"thread_id": ...}}, *, context=None,
       durability="sync"|"async"|"exit", ...)

SqliteSaver.from_conn_string(conn_string) -> Iterator[SqliteSaver]   # 上下文管理器
```

**三条已验证的事实**（决定了状态模型能不能成立）：

1. **pydantic 模型可以活在 state 里并挺过 checkpoint 往返** —— 实测 `SqliteSaver`
   存取后类型仍是原类。
2. **`Annotated[list[X], operator.add]` reducer 生效** —— 多个节点各返回一段，
   自动累加。
3. **不可序列化的依赖走 `context_schema` 不进 state** —— 实测传入含 lambda 的对象
   正常工作，未进 checkpoint。

**同时炸出一个坑（笔记素材）**：往返时 LangGraph 会警告
`Deserializing unregistered type ... This will be blocked in a future version`，
需通过 `allowed_msgpack_modules` 显式注册自定义类型。手写版的
`io.write_stage` 存普通 JSON，没有这种隐式契约。

## 3. 先修三个域缺陷（共用层，两个实现都受益）

这三条跟框架无关，修在共用层里，避免在两个编排里各修一遍。

| 缺陷 | 修法 | 依据 |
|---|---|---|
| 剪枝没反推 `CYCLE` | 同年级候选只保留单向：`grade_start` 相等时，仅当 `other.draft_id < target.draft_id` 才进候选池 | 首次真实运行实测：模型对两个同年级节点提出双向边，靠 judge 兜住而非剪枝挡住 |
| 淘汰制造孤儿无感知 | review 后统计"原本有前置、因本次淘汰而失去全部前置"的节点，记 `ORPHANED_BY_REJECTION`（WARNING 语义，仅记账不丢弃） | 实测 `小数的意义` 被淘汰后，两个后继静默变孤儿 |
| `run_all` 误报 `CONSISTENCY_SKIPPED` | `run_pipeline` 把 review 层用过的 name judge 传给 `run_all` | review 层明明跑过 name judge，最终报告却说"已跳过" |

**同年级定方向为什么按 `draft_id` 字典序**：简单、确定、零额外调用。
代价是武断 —— 真实的先修方向可能相反。替代方案是让模型选方向（多一次调用）。
本轮取简单解，并在 `edges.py` 注释里写明这是取舍而非定论。

## 4. A 阶段：对等实现

### 状态模型

```python
class PipelineState(TypedDict):
    chunks: list[Chunk]
    drafts: list[TopicDraft]
    deduped: list[TopicDraft]
    merges: list[Merge]
    proposed: dict[str, list[ProposedEdge]]
    reviewed: list[TopicDraft]
    kept_edges: dict[str, list[ProposedEdge]]
    outcomes: list[ReviewOutcome]
    drops: Annotated[list[DropRecord], operator.add]
    findings: list[Finding]
```

`drops` 是唯一带 reducer 的字段，其余为覆盖语义。

### 节点映射

六个 Node 与六层一一对应，每个 Node 只做「从 state 取输入 → 调那层纯函数 →
返回 delta」。**不允许在 Node 里写业务逻辑** —— 一旦写了，对比就不公平了
（变成"手写版 vs 框架版+重构"）。

```
START → chunk → extract → dedupe → edges → review → assemble → END
```

依赖注入：`StateGraph(PipelineState, context_schema=PipelineDeps)`，
直接复用手写版现有的 `PipelineDeps` dataclass，`invoke(..., context=deps)`。

每个 Node 挂：
- `retry_policy=RetryPolicy(max_attempts=3, retry_on=_retry_on)`，其中：

  ```python
  _PROGRAMMING_ERRORS = (AttributeError, TypeError, NameError, KeyError)

  def _retry_on(exc: Exception) -> bool:
      """程序 bug 不该被重试 —— 重试三次只会把同一个 bug 犯三遍。
      与手写版收窄 except 的策略同源（见 extract.py）。"""
      return not isinstance(exc, _PROGRAMMING_ERRORS)
  ```
- `error_handler=` —— 节点级失败兜底
- 编译时 `checkpointer=SqliteSaver`

### 落盘

LangGraph 版**仍然逐层落盘**（复用 `io.write_stage`），因为"中间产物可人眼检查"
是项目原则，不是手写版的实现细节。checkpoint 是框架内部格式，替代不了它。

**这是第一条对比素材**：手写版有五处显式 `io.append_drops`；LangGraph 版声明一次
`Annotated[..., operator.add]`，累加语义成了类型的一部分。省了代码，
但丢了"每层跑完立刻落盘"的时序保证 —— 需要在 Node 里显式补落盘调用。

## 5. 故障注入与度量装置

### 注入

`pipeline/faults.py` 提供包裹器，把 `PipelineDeps` 里每个可调用项换成
「计数 + 可控失败」的代理：

```python
@dataclass
class FaultSpec:
    target: str        # "extractor" / "fidelity_judge" / "edge_proposer" ...
    fail_on_call: int  # 第几次调用时失败（1-based）
    exc: type[Exception]
    times: int = 1     # 连续失败几次

def wrap_deps(deps: PipelineDeps, specs: list[FaultSpec]) -> tuple[PipelineDeps, CallCounter]
```

**零生产代码改动** —— 因为 deps 本来就是依赖注入的。这本身是笔记素材：
当初为可测性做的 DI，现在直接变成了实验装置。

### 三种故障

| 故障 | 注入方式 | 考察 |
|---|---|---|
| 429 限流 | `fidelity_judge` 第 2 次调用抛 `RateLimitError`，连续 1 次 | 重试能否自愈 |
| 中途崩溃 | `edge_proposer` 第 1 次调用抛 `RuntimeError`，连续 99 次（不可恢复） | 恢复时从哪里续跑 |
| 节点超时 | `extractor` 第 2 次调用 sleep 超过 `timeout` | 超时策略与重试的交互 |

⚠️ **第三种故障两边不对等，这本身是结果之一**：手写版**根本没有超时概念**
（`run_pipeline` 里没有任何 timeout），注入 sleep 后它只会一直等。
LangGraph 版有 `add_node(timeout=)`。所以这一格的结论不是"谁更快恢复"，
而是"一个能力手写版压根没有，需要多少代码才能补上" —— 记录补齐它所需的代码量，
与框架白送的对比。

### 度量四项

1. **恢复后各层被重新调用的次数**（`CallCounter` 按 target 分别计数）
2. **重复调用总数** —— token 消耗的代理量
3. **墙钟时间**
4. **编排层代码行数** —— 口径必须先定死，否则这个数字可以随便捏：
   - 手写版计入：`run.py` 的 `run_pipeline` 函数体（含各层调用、落盘、
     `surviving_edges` 过滤与丢边记账）
   - LangGraph 版计入：`graph.py` 里 `PipelineState` 定义 + 六个 Node 函数体 +
     建图与 `compile` 调用
   - **两边都不计**：六层纯函数、`PipelineDeps`、CLI 参数解析、import
   - 统计方式：`cloc` 或手工数**非空非注释行**，两边同一把尺子

### 为什么用 fake 而非真模型

可复现、免费、故障位置精确可控。代价是测不出真实限流的行为
（真实 429 常伴随 `Retry-After` 头、并发退避等）。这条限制要写进笔记，
不能让读者以为这是生产环境实测。

## 6. B 阶段：`Send` 扇出

`extract` 与 `review` 改为 `Send` 扇出到条目级（每 chunk 一个抽取任务、
每 draft 一个审核任务），其余节点不变。

考察点：429 恢复后，A 阶段重跑**整层**（含已成功的条目），
B 阶段只重跑**失败那一条**。度量同 §5，单独成章。

**必须在笔记里声明**：B 阶段已不是"同一需求的两种实现"——架构变了。
它回答的是另一个问题（框架解锁了什么），不能和第一章的数据混着读。

## 7. 对比笔记结构

`docs/langgraph-vs-handwritten.md`：

- **第一章 对等对比**：框架省了什么（重试/重入/失败隔离各自的代码行数）、
  强加了什么（msgpack 类型注册、checkpoint 不可人眼读、state schema 的表达约束、
  出错时调用栈变深、多一层版本依赖）
- **第二章 额外解锁**：条目级 checkpoint 省下的重复调用量
- **第三章 什么时候不该用它**：结论必须由前两章的数据支撑，不写观点性断言

## 8. 测试策略

**硬标准：现有全 fake 端到端测试（含跨层守恒断言）必须对两个实现都绿。**
这是"对等实现"的定义 —— 测试对实现无感知，才谈得上对比。

做法：把 `test_run.py` 里的端到端用例参数化，`@pytest.mark.parametrize("engine", ["handwritten", "langgraph"])`。

新增测试：`faults.py` 的包裹器本身要有单测（计数准不准、失败位置对不对）——
实验装置不可信，实验数据就不可信。

## 9. 不做的事（YAGNI）

LangGraph Platform / Studio、异步执行、Postgres checkpointer、
`interrupt` 人在环（超出"同一需求两种实现"的对比前提）、真模型上量、
LangGraph 版的性能调优。

## 10. 已知风险

| 项 | 状态 |
|---|---|
| LangGraph 1.2 发布于 2026-05，API 仍在快速变化 | 本文 §2 为 2026-07-26 实测；动手前应重新核实 |
| 自定义类型进 checkpoint 需注册 `allowed_msgpack_modules`，未来版本强制 | 实现时显式注册，并把这条写进笔记的"框架强加了什么" |
| 受控实验用 fake，测不出真实限流行为 | 已在 §5 声明，笔记中须复述 |
| 同年级边按 `draft_id` 定方向是武断解 | 已在 §3 说明取舍，注释中标注 |
