"""流水线内部类型的契约测试。

最要紧的一条：DraftContent 是**给模型的 input_schema**，
它里面不能出现任何该由代码填的字段（id / provenance / standard_codes）。
这条边界一旦破了，就等于让模型自己声明自己可信 —— 正是本项目要修掉的缺陷。
"""

import pytest
from pydantic import ValidationError

from cn_curriculum_graph.pipeline.models import (
    Chunk,
    DraftContent,
    DropRecord,
    TopicDraft,
)


def _content(**kw) -> DraftContent:
    defaults = dict(
        name="小数的意义",
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
    defaults.update(kw)
    return DraftContent(**defaults)


def test_draft_content_schema_excludes_code_owned_fields():
    schema_fields = set(DraftContent.model_json_schema()["properties"])
    # 这四个字段由代码填，绝不能出现在给模型的 schema 里
    assert schema_fields.isdisjoint({"id", "provenance", "standard_codes", "chunk_id"})


def test_draft_content_forbids_extra_fields():
    with pytest.raises(ValidationError):
        _content(confidence=0.9)


def test_draft_content_requires_at_least_one_evidence():
    with pytest.raises(ValidationError):
        _content(evidence=[])


def test_topic_draft_carries_pipeline_owned_fields():
    draft = TopicDraft(
        draft_id="src#001-0",
        chunk_id="src#001",
        standard_codes=["3.1.2"],
        content=_content(),
    )
    assert draft.content.name == "小数的意义"
    assert draft.standard_codes == ["3.1.2"]


def test_chunk_requires_standard_code():
    with pytest.raises(ValidationError):
        Chunk(id="src#001", text="正文", source_file="src.md", ordinal=1)


def test_drop_record_records_stage_and_reason():
    rec = DropRecord(stage="chunk", ref="src#003", reason="NO_STANDARD_CODE")
    assert rec.detail == ""
