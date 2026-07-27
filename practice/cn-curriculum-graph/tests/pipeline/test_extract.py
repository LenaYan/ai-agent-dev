"""抽取层。测试只验"接线"：喂对参数、把返回翻译成 TopicDraft、失败不中断整批。

抽得准不准是另一回事，靠人眼看中间产物 —— 本轮不承诺内容正确性，
为它写断言等于假装能验证。
"""

from types import SimpleNamespace

import pytest

from cn_curriculum_graph.errors import ToolCallMissingError
from cn_curriculum_graph.pipeline.extract import (
    EXTRACT_TOOL_NAME,
    DeepSeekExtractor,
    extract_all,
)
from cn_curriculum_graph.pipeline.models import Chunk, DraftBatch, DraftContent


def _chunk(ordinal: int = 1, text: str = "能理解小数的意义") -> Chunk:
    return Chunk(
        id=f"math#{ordinal:03d}",
        text=text,
        standard_code="3.1.2",
        source_file="math.md",
        ordinal=ordinal,
    )


def _content(name: str = "小数的意义") -> DraftContent:
    return DraftContent(
        name=name,
        description="理解小数表示十进制分数",
        type="conceptual",
        subject="数学",
        domain="数与代数",
        grade_start=4,
        grade_end=4,
        evidence=["能说出 0.3 表示十分之三"],
        assessment_prompt="0.3 是什么意思？",
        source_span="能理解小数的意义",
    )


def test_attaches_chunk_id_and_standard_code_to_every_draft():
    def extractor(chunk):
        return DraftBatch(drafts=[_content("甲"), _content("乙")])

    drafts, drops = extract_all([_chunk()], extractor)

    assert [d.content.name for d in drafts] == ["甲", "乙"]
    # 编号来自 chunk，不来自模型
    assert all(d.standard_codes == ["3.1.2"] for d in drafts)
    assert all(d.chunk_id == "math#001" for d in drafts)
    assert drops == []


def test_draft_ids_are_deterministic():
    def extractor(chunk):
        return DraftBatch(drafts=[_content("甲"), _content("乙")])

    drafts, _ = extract_all([_chunk()], extractor)

    # index 补零到两位（见 test_draft_id_lexical_order_matches_extraction_order_past_nine
    # 的动机）：math#001-00 / math#001-01，而非未补零的 math#001-0 / math#001-1。
    assert [d.draft_id for d in drafts] == ["math#001-00", "math#001-01"]


def test_draft_id_lexical_order_matches_extraction_order_past_nine():
    """index 必须补零，否则字典序与抽取序系统性反相关。

    chunk.id 本身补零到三位（f"{stem}#{ordinal:03d}"），但 draft_id 的 index
    若是裸整数，"...-10" 会因为字符串比较在 "...-2" 之前排序 —— 一旦一条
    条目抽出 ≥10 个知识点，第 10/11/12 个会稳定地排到第 2 个前面。这不是
    偶发问题：edges.py 的 candidate_prerequisites 依赖"同年级节点按
    draft_id 字典序 ≈ 抽取序"来定先后方向，前提一旦失效，方向就系统性地错。
    """

    def extractor(chunk):
        return DraftBatch(drafts=[_content(f"第{i}个") for i in range(12)])

    drafts, _ = extract_all([_chunk()], extractor)

    draft_ids = [d.draft_id for d in drafts]
    assert draft_ids == sorted(draft_ids), (
        "draft_id 按字典序排序应与抽取顺序一致，"
        f"实际抽取序={draft_ids}，字典序={sorted(draft_ids)}"
    )


def test_a_failing_chunk_is_dropped_without_stopping_the_batch():
    def extractor(chunk):
        if chunk.ordinal == 1:
            raise RuntimeError("API 炸了")
        return DraftBatch(drafts=[_content("乙")])

    drafts, drops = extract_all([_chunk(1), _chunk(2)], extractor)

    assert [d.content.name for d in drafts] == ["乙"]
    assert len(drops) == 1
    assert drops[0].stage == "extract"
    assert drops[0].reason == "EXTRACT_FAILED"
    assert "API 炸了" in drops[0].detail


def test_extract_all_reraises_programming_errors_instead_of_recording_a_drop():
    """AttributeError/TypeError/NameError/KeyError 是程序 bug，不该被当成 API
    失败悄悄记进 dropped.json —— 那会把真实缺陷伪装成"这批数据不行"。"""

    def extractor(chunk):
        raise AttributeError("拼错了属性名")

    import pytest

    with pytest.raises(AttributeError, match="拼错了属性名"):
        extract_all([_chunk()], extractor)


def test_grade_range_inverted_draft_is_dropped_without_crashing_the_batch():
    """DraftContent 只有 ge/le 边界，没有顺序校验 —— grade_end < grade_start
    这种模型最常见的填反区间，必须在 extract 层单独丢弃该 draft 并记账，
    而不是让它一路穿到 assemble 才用 pydantic ValidationError 炸掉整条流水线。
    同一批次里其他合格的 draft 不受影响。"""

    def extractor(chunk):
        good = _content("甲")
        bad = _content("乙").model_copy(update={"grade_start": 5, "grade_end": 3})
        return DraftBatch(drafts=[good, bad])

    drafts, drops = extract_all([_chunk()], extractor)

    assert [d.content.name for d in drafts] == ["甲"]
    assert len(drops) == 1
    assert drops[0].stage == "extract"
    assert drops[0].reason == "GRADE_RANGE_INVERTED"
    assert "乙" in drops[0].detail


def test_a_chunk_yielding_nothing_is_recorded():
    def extractor(chunk):
        return DraftBatch(drafts=[])

    drafts, drops = extract_all([_chunk()], extractor)

    assert drafts == []
    assert drops[0].reason == "NO_DRAFTS"


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", name=EXTRACT_TOOL_NAME, input=tool_input)
            ]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_extractor_forces_the_batch_tool_deterministically():
    recorder: dict = {}
    client = _fake_client(recorder, {"drafts": [_content().model_dump(mode="json")]})

    batch = DeepSeekExtractor(client=client)(_chunk())

    assert [d.name for d in batch.drafts] == ["小数的意义"]
    assert recorder["tool_choice"] == {"type": "tool", "name": EXTRACT_TOOL_NAME}
    assert recorder["thinking"] == {"type": "disabled"}
    assert recorder["temperature"] == 0
    (tool,) = recorder["tools"]
    assert tool["input_schema"] == DraftBatch.model_json_schema()


def _collect_property_names(schema: dict) -> set[str]:
    """递归收集 schema 里所有 "properties" 下的字段名（含 $defs 被引用的子 schema）。

    只看属性名，不把整份 schema 序列化成字符串去 grep —— `DraftContent` 的类
    docstring 里提到了 "chunk_id" "standard_codes" 等字段名作为解释性文字，
    会出现在 schema 的 description 里，字符串匹配会把这些误报成字段泄漏。
    """
    names: set[str] = set()

    def walk(node: object) -> None:
        if isinstance(node, dict):
            props = node.get("properties")
            if isinstance(props, dict):
                names.update(props.keys())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(schema)
    return names


def test_extract_tool_schema_never_exposes_code_owned_fields():
    """核心红线：给模型的 input_schema 里绝不能出现这五个代码专属字段。

    `DraftBatch` 的 schema 用 `$defs` 引用了内层 `DraftContent`（以及更内层的
    `Misconception`），只查顶层 properties 抓不到内层泄漏，所以要递归查。
    这条测试跟 `tests/pipeline/test_models.py` 里
    `test_draft_content_schema_excludes_code_owned_fields` 的区别：那条锁的是
    `DraftContent` 单独导出的 schema，这条锁的是 extract 层真正传给模型的
    `DraftBatch`（外层）schema —— 往 `DraftBatch` 本身误加字段，内层测试测不到。
    """
    schema = DraftBatch.model_json_schema()
    forbidden = {"chunk_id", "standard_codes", "draft_id", "id", "provenance"}

    assert _collect_property_names(schema).isdisjoint(forbidden)


def test_deepseek_extractor_raises_when_the_model_skips_the_tool():
    """异常类型从 `ValueError` 改判为 `ToolCallMissingError`（见
    `tests/test_error_taxonomy.py` 与 `cn_curriculum_graph/errors.py`）：
    "模型没按要求调工具"是远端服务的**瞬时**协议违约，不是确定性的值错误，
    而 `pipeline/graph.py` 的 `retry_on` 把全部 `ValueError` 都排除在重试
    之外——留在 ValueError 家族里等于永久放弃对这类故障的重试。
    """
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(type="text", text="我不知道")]
            )
        )
    )

    with pytest.raises(ToolCallMissingError, match=EXTRACT_TOOL_NAME):
        DeepSeekExtractor(client=client)(_chunk())
