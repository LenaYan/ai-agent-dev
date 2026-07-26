"""抽取层。测试只验"接线"：喂对参数、把返回翻译成 TopicDraft、失败不中断整批。

抽得准不准是另一回事，靠人眼看中间产物 —— 本轮不承诺内容正确性，
为它写断言等于假装能验证。
"""

from types import SimpleNamespace

import pytest

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

    assert [d.draft_id for d in drafts] == ["math#001-0", "math#001-1"]


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


def test_deepseek_extractor_raises_when_the_model_skips_the_tool():
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(type="text", text="我不知道")]
            )
        )
    )

    with pytest.raises(ValueError, match=EXTRACT_TOOL_NAME):
        DeepSeekExtractor(client=client)(_chunk())
