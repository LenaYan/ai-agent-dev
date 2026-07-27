# 异常分类与重试可达性

> 起因：接真模型上量前重评 `retry_on` 排除全部 `ValueError` 这条决定的**误伤面**。
> 时间：2026-07-27。代码依据：`pipeline/graph.py::NODE_RETRY_EXCLUDED_ERRORS`、
> `errors.py`、`tests/test_error_taxonomy.py`。

## 0. 一句话结论

**误伤面今天是零，但零是"恰好"来的**——靠的是各层 catch 边界的当前形状，
不是任何结构约束。所以动作不是收窄排除集合，而是**把唯一一个语义上不属于
"确定性错误"的异常搬出 `ValueError`**（`ToolCallMissingError`），让
"`ValueError` = 不重试"从碰巧成立变成按构造成立。

## 1. 为什么这条规则值得重评

`retry_on` 是按**类型**判决的：

```python
NODE_RETRY_EXCLUDED_ERRORS = PROGRAMMING_ERRORS + (ValueError,)
def retry_on(exc): return not isinstance(exc, NODE_RETRY_EXCLUDED_ERRORS)
```

判决依据只有类型，没有抛出位置、没有错误消息。这意味着**同一个内置类型被
用来表达两种相反语义时，重试策略必然对其中一种是错的**。而 `ValueError`
恰好是 Python 里最容易被滥用成"通用错误"的那个类型。

## 2. 全部 `ValueError` 抛出点（逐个走完）

| # | 位置 | 语义 | 重试有用吗 | 能摸到 `retry_on` 吗 |
|---|---|---|---|---|
| 1 | `review.py:163` `fidelity_judges` 为空 | 确定性配置错误 | 无用 | ✅ 可达（循环前 raise，不被任何 try 包裹） |
| 2 | `review.py:168` `name_judges` 为空 | 同上 | 无用 | ✅ 可达 |
| 3 | `review.py:280` `edge_judges` 为空 | 同上 | 无用 | ✅ 可达 |
| 4 | `assemble.py:126` topic id 碰撞 | 确定性契约错误 | 无用 | ❌ `assemble` Node 没挂 `retry_policy` |
| 5 | `assemble.py:174/181` 未知 draft id | 同上 | 无用 | ❌ 同上 |
| 6 | `models.py:117` 年级区间倒挂 | 确定性数据错误 | 无用 | ❌ 只在 assemble 里构造 |
| 7 | `models.py:136/156` 自环 | 同上 | 无用 | ❌ 同上 |
| 8 | `io.py` 内的 `json.JSONDecodeError` | dropped.json 被改坏 | 无用 | ✅ **可达**（见下） |
| 9 | `io.py` 内的 `UnicodeDecodeError` | 文件非 UTF-8 | 无用 | ✅ 可达 |
| 10 | pydantic `ValidationError`（`ReviewOutcome` 等在 try 外构造） | 确定性数据错误 | 无用 | ✅ 可达 |
| 11 | `extract.py:82` / `dedupe.py:132` / `edges.py:119` / `review.py:119` / `judges/deepseek_judge.py:69`「模型未调用工具」 | **远端服务协议违约** | **有用** | ❌ 被逐条 try/except 吞成 `DropRecord` |
| 12 | `cli.py:44` 未知 judge、`faults.py`、`compare_orchestration.py:199` | 参数校验 | — | ❌ 不在 Node 路径上 |
| 13 | `graph.py::_ensure_consistent_resume` | 确定性配置错误 | 无用 | ❌ 在 Node 之外（`_ainvoke` 里）抛 |

第 8/9/10 条容易被漏掉，值得单独说：`io.append_drops` 是**直接在 Node 函数体里
调用的**（`node_extract`/`node_dedupe`/`node_edges`/`node_review` 各调 1~4 次），
不在任何逐条 try/except 内。它会 `json.loads(path.read_text(encoding="utf-8"))`
读已有的 dropped.json——文件被外部改坏、或磁盘上是半截写入，抛出的
`JSONDecodeError`/`UnicodeDecodeError` 会一路冒到 Node 外层，**正正撞上
`retry_on`**。它们都是 `ValueError` 的子类，也都是确定性错误。

## 3. 误伤面到底有多大

按上表：**可达 `retry_on` 的 ValueError（1/2/3/8/9/10）全部是确定性错误，
排除它们是对的；唯一"重试有用"的第 11 类今天根本够不着这条规则。**
所以静态地看，误伤面 = 0。

但这个 0 完全依赖一件事：第 11 类被 `extract_all`/`dedupe`/`propose_all`/
`review_drafts`/`review_edges` 的逐条 `except Exception` 拦住了。这个 catch
边界是**为了"单条失败不中断整批"而存在的**，不是为了保护重试语义——将来
任何一次对它的调整（比如为了区分故障类型而收窄 except，或新增一处不带
try 的调用点）都会让误伤在无人察觉时出现。

**靠"恰好不可达"支撑的正确性不是正确性，是运气。**

## 4. 为什么不收窄排除集合

直觉方案是"只排除自己定义的配置错误类型"，例如引入 `ConfigError(ValueError)`
然后 `NODE_RETRY_EXCLUDED_ERRORS = PROGRAMMING_ERRORS + (ConfigError,)`。
**这个方向是错的**：它会把第 8/9/10 类（`JSONDecodeError`、
`UnicodeDecodeError`、pydantic `ValidationError`）变成可重试的。这三类都是
确定性的——同一个坏文件读三遍还是坏的，同一份不合法数据校验三遍还是不合法
——重试只是浪费三倍的退避时间，且在真实场景里会把"文件坏了"这个诊断信息
延迟三个退避周期才呈现给人。

## 5. 实际的修法：把异常搬家，而不是把规则收窄

新增 `cn_curriculum_graph/errors.py::ToolCallMissingError(RuntimeError)`，
六处「模型未调用 XXX 工具」全部改抛它。

选 `RuntimeError` 作基类而不是裸 `Exception`，是为了**保持今天的行为完全不变**：
`RuntimeError` 不在 `PROGRAMMING_ERRORS` 里，各层的 `except Exception` 照样
接得住、照样转成 `DropRecord`。这次改动只改变一件事——**万一它冒泡到 Node
外层时的重试判定**（从"排除"变成"重试"）。

改完之后的类型语义表：

| 类别 | 该不该重试 | 类型 |
|---|---|---|
| 程序 bug | 不 | `PROGRAMMING_ERRORS` = AttributeError / TypeError / NameError / KeyError |
| 确定性的配置 / 契约 / 数据错误 | 不 | `ValueError`（含 JSONDecodeError / UnicodeDecodeError / pydantic ValidationError 等子类） |
| 远端服务的瞬时故障 | 是 | `ToolCallMissingError`，以及 SDK 自己的限流 / 超时 / 网络异常 |

## 6. 遗留：这一层重试本来就够不着最常见的故障

必须和 `graph.py` 的 I1 结论一起读，否则容易高估这次改动的价值：

- 六层函数一律"逐条 try/except，非程序错误转 `DropRecord` 不冒泡"，所以
  **API 限流 / 超时 / 网络抖动这类最常见的 LLM 故障从来不会触发 Node 级
  `RetryPolicy`**——包括改判之后的 `ToolCallMissingError`。
- 也就是说，这次改动的直接收益是 **0**（今天没有任何一条执行路径的行为会
  变），收益全部在未来：catch 边界一旦调整，重试判决会自动落到对的一边。

**真正缺的那件事仍然是"逐条重试 + 退避"**，它在 README 的待办里，且按 I1 的
实测结论，正确的重试粒度是**条目级**（手写版逐条 try/except 那个粒度），
不是 Node 级。Node 级 `RetryPolicy` 在这个项目里只覆盖"整层调用彻底失败"
这个较窄的故障类别。

## 7. 对应的测试

`tests/test_error_taxonomy.py`：

- `test_tool_call_missing_is_not_a_value_error` —— 类型契约本身
- `test_tool_call_missing_is_retryable_but_config_errors_are_not` —— 把"该不该
  重试"钉成断言，含 JSONDecodeError / UnicodeDecodeError 两个易漏的 `ValueError` 子类
- `test_every_forced_tool_call_site_raises_tool_call_missing` —— 参数化覆盖全部
  六个强制工具调用点，防"新增第七处时忘了改"
- `test_tool_call_missing_still_becomes_a_droprecord_not_a_crash` —— 行为不变的回归锁
- `test_corrupt_dropped_json_raises_a_value_error_from_inside_a_retryable_node` ——
  证明第 8 类真的可达，上面的可达性分析不是纸上谈兵
