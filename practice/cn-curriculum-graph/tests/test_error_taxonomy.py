"""异常分类：什么该重试、什么不该，以及为什么按类型而不是按位置分。

**这组测试来自对"`retry_on` 排除全部 `ValueError`"这条决定的重评。**
原来的取舍写在 `pipeline/graph.py` 的 `NODE_RETRY_EXCLUDED_ERRORS` 注释里，
自己也标注了一条已知代价：五处"模型未调用 XXX 工具"抛的也是 `ValueError`，
而那是**唯一一类重试可能真的有用**的情形，只是"今天恰好被各层的逐条
try/except 吞掉、摸不到这条排除规则"。

重评把全部 `ValueError` 抛出点走了一遍（`docs/error-taxonomy.md` 有完整
清单与可达性分析），结论是：

- **今天的误伤面确实是零**，但零是"恰好"来的，靠的是 catch 边界的当前形状，
  不是靠任何结构约束。注释里那句"如果未来六层函数的 catch 边界发生变化，
  这一点需要重新评估"，等于把一颗雷交给了未来的自己。
- **真正的病根不是排除规则太宽，是那五处用错了异常类型。** "模型没按要求
  调工具"不是值错误、不是配置错误，它是**远端服务的协议违约**——和限流、
  超时、网络抖动同类，重试一次很可能就好了。把它塞进 `ValueError`，等于把
  一个瞬时故障伪装成确定性错误。
- 所以修法不是收窄排除集合（那会把 `JSONDecodeError`/`UnicodeDecodeError`/
  `ValidationError` 这些**真·确定性**的 `ValueError` 子类变成可重试的，纯亏），
  而是**把唯一一个不属于这一类的异常搬出 `ValueError`**：新增
  `ToolCallMissingError(RuntimeError)`。

改完之后，"`ValueError` = 确定性错误，一律不重试"从**碰巧成立**变成
**按构造成立**。
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from cn_curriculum_graph.errors import ToolCallMissingError
from cn_curriculum_graph.judges.deepseek_judge import DeepSeekJudge
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline.dedupe import DeepSeekSameTopicJudge
from cn_curriculum_graph.pipeline.edges import DeepSeekEdgeProposer
from cn_curriculum_graph.pipeline.extract import DeepSeekExtractor, extract_all
from cn_curriculum_graph.pipeline.graph import retry_on
from cn_curriculum_graph.pipeline.models import Chunk, DraftContent, ProposedEdge, TopicDraft
from cn_curriculum_graph.pipeline.review import DeepSeekEdgeJudge, DeepSeekFidelityJudge


def _client_returning_no_tool_use():
    """模型没调工具，只回了一段自由文本 —— DeepSeek 实测出现过的情形
    （见 memory：v4 默认开 thinking 时不接受强制 tool_choice）。"""

    def create(**kwargs):
        return SimpleNamespace(content=[SimpleNamespace(type="text", text="否")])

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def _draft(name: str = "分数的初步认识") -> TopicDraft:
    return TopicDraft(
        draft_id="d1", chunk_id="c1", standard_codes=["3.1.1"],
        content=DraftContent(
            name=name, description="描述", type="conceptual", subject="数学",
            domain="数与代数", grade_start=3, grade_end=3, evidence=["能举例"],
            assessment_prompt="说说？", source_span="原文",
        ),
    )


def test_tool_call_missing_is_not_a_value_error():
    """类型契约：它必须**不是** ValueError，否则整个重评白做。

    这也是它被 `retry_on` 放行的唯一依据 —— 排除集合是按类型判的，
    不是按错误消息或抛出位置判的。
    """
    exc = ToolCallMissingError("模型未调用 xxx 工具")

    assert not isinstance(exc, ValueError)
    assert isinstance(exc, Exception)


def test_tool_call_missing_is_retryable_but_config_errors_are_not():
    """把"该不该重试"这条判断本身钉成断言。

    左边（可重试）：远端服务协议违约，重试一次很可能就好。
    右边（不可重试）：确定性错误，重试三次只是把同一个错犯三遍。
    注意 `JSONDecodeError`/`UnicodeDecodeError`/pydantic `ValidationError`
    都是 `ValueError` 的子类，且都真的能在 Node 体里冒出来（前两者来自
    `io.append_drops` 读 dropped.json，见 `test_corrupt_dropped_json_...`），
    它们留在排除集合里是对的 —— 这正是"不该把排除集合收窄成一个自定义
    类型"的原因。
    """
    assert retry_on(ToolCallMissingError("模型未调工具")) is True

    assert retry_on(ValueError("fidelity_judges 为空")) is False
    assert retry_on(json.JSONDecodeError("bad", "doc", 0)) is False
    assert retry_on(UnicodeDecodeError("utf-8", b"\xff", 0, 1, "invalid")) is False


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(
            lambda c: DeepSeekExtractor(client=c)(
                Chunk(id="c1", text="正文", standard_code="3.1.1", source_file="m.md", ordinal=1)
            ),
            id="extract",
        ),
        pytest.param(
            lambda c: DeepSeekSameTopicJudge(client=c)(_draft("甲"), _draft("乙")),
            id="dedupe",
        ),
        pytest.param(
            lambda c: DeepSeekEdgeProposer(client=c)(_draft("甲"), [_draft("乙")]),
            id="edges",
        ),
        pytest.param(lambda c: DeepSeekFidelityJudge(client=c)(_draft()), id="review-fidelity"),
        pytest.param(
            lambda c: DeepSeekEdgeJudge(client=c)(
                _draft(),
                ProposedEdge(prerequisite_draft_id="d0", strength="hard", reason="理由"),
            ),
            id="review-edge",
        ),
        pytest.param(
            lambda c: DeepSeekJudge(client=c)(name="甲", description="乙"), id="name-judge"
        ),
    ],
)
def test_every_forced_tool_call_site_raises_tool_call_missing(call):
    """六处强制工具调用点必须**全部**改过来，漏一处这条就红。

    参数化而不是写六个函数：这类"同一约定散落在多个模块"的规则，逐个手写
    测试的下场是新增第七处时没人记得加测试。这里至少让"漏改"在已有的
    六处里无所遁形。
    """
    with pytest.raises(ToolCallMissingError):
        call(_client_returning_no_tool_use())


def test_tool_call_missing_still_becomes_a_droprecord_not_a_crash():
    """行为不变的回归锁：换了异常类型之后，各层"逐条 try/except"仍然接得住。

    `ToolCallMissingError` 继承 `RuntimeError` 而不是 `PROGRAMMING_ERRORS`
    里那四个（AttributeError/TypeError/NameError/KeyError），所以它会走
    `except Exception` 那条分支、被转成 DropRecord 而不是掀翻整批 ——
    如果哪天有人图省事让它继承 `TypeError`，这条会立刻红。
    """
    chunk = Chunk(id="c1", text="正文", standard_code="3.1.1", source_file="m.md", ordinal=1)

    drafts, drops = extract_all([chunk], DeepSeekExtractor(client=_client_returning_no_tool_use()))

    assert drafts == []
    assert [d.reason for d in drops] == ["EXTRACT_FAILED"]
    assert "ToolCallMissingError" in drops[0].detail


def test_corrupt_dropped_json_raises_a_value_error_from_inside_a_retryable_node(tmp_path):
    """证明"`ValueError` 子类真的能在挂了 RetryPolicy 的 Node 体里冒出来"，
    上面那条 `retry_on(...) is False` 才不是空谈。

    `io.append_drops` 被 node_extract/node_dedupe/node_edges/node_review
    直接调用（不在任何逐条 try/except 里），它会 `json.loads` 已有的
    dropped.json。文件被外部改坏时抛的 `json.JSONDecodeError` 是
    `ValueError` 子类，会一路冒到 Node 外层撞上 `retry_on`——而它是彻头彻尾
    的确定性错误，重试三次只会读同一个坏文件三遍。
    """
    path = tmp_path / "dropped.json"
    path.write_text("{ 这不是合法 JSON", encoding="utf-8")

    with pytest.raises(ValueError) as caught:
        io.append_drops(path, [_draft().content])

    assert retry_on(caught.value) is False
