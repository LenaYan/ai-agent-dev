"""跨包共用的异常类型。

单独成一个模块（而不是塞进 `pipeline/models.py`）的原因很朴素：
`judges/` 与 `pipeline/` 都要用它，而 `judges/` 不依赖 `pipeline/`，
把它放进任一边都会制造一条不该有的依赖边。

**异常分类的原则**（重评 `retry_on` 排除规则时确立，完整论证见
`docs/error-taxonomy.md` 与 `tests/test_error_taxonomy.py`）：

| 类别 | 该不该重试 | 用什么类型表达 |
|---|---|---|
| 程序 bug | 不 | `PROGRAMMING_ERRORS`（AttributeError/TypeError/NameError/KeyError） |
| 确定性的配置/契约/数据错误 | 不 | `ValueError`（含 `JSONDecodeError`/`UnicodeDecodeError`/pydantic `ValidationError` 等子类） |
| 远端服务的瞬时故障 | 是 | 本模块的 `ToolCallMissingError`，以及 SDK 自己的限流/超时/网络异常 |

关键在于**类型即语义**：`retry_on` 只看类型，所以"这个错误该不该重试"
必须能从类型上读出来，不能靠抛出位置或错误消息。
"""

from __future__ import annotations


class ToolCallMissingError(RuntimeError):
    """强制 `tool_choice` 之后，模型仍然没有调用指定工具。

    **为什么不是 `ValueError`**（这是一次刻意的改判，原来六处全用
    `ValueError`）：它描述的不是"某个值不对"，而是**远端服务违反了协议**
    ——我们明确要求 `tool_choice={"type":"tool","name":X}`，对方却回了自由
    文本。这和限流、超时、网络抖动是同一类东西：**同样的输入再发一次，
    很可能就正常了**。

    这个区分不是命名洁癖，它直接决定重试行为：`pipeline/graph.py` 的
    `retry_on` 排除全部 `ValueError`（因为这个项目里 `ValueError` 一律
    表示确定性错误），把本异常留在 `ValueError` 家族里，等于永久放弃对
    这类故障的重试。

    **为什么继承 `RuntimeError` 而不是裸 `Exception`**：`RuntimeError`
    不在 `PROGRAMMING_ERRORS` 里，因此各层"逐条 try/except"的
    `except Exception` 分支仍会接住它、转成 `DropRecord`，行为与改判之前
    完全一致——这次改动只改变"万一它冒泡到 Node 外层时的重试判定"，
    不改变今天任何一条已有的执行路径。

    实测背景（memory 有记）：DeepSeek v4 默认开 thinking，而思考模式不接受
    强制 `tool_choice`，会直接返回自由文本。这类问题一半靠配置修
    （`thinking={"type":"disabled"}`），另一半是真的偶发。
    """
