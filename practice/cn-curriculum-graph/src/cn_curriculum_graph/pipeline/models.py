"""生成流水线的内部类型。

与 `cn_curriculum_graph.models`（对外 schema）分开：这些类型只活在管道内部，
assemble 之后就丢掉。分开的好处是能把"字段归属"做成结构上的约束而非约定 ——
`DraftContent` 就是给模型的 input_schema，它装不下代码该填的字段。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cn_curriculum_graph.models import GRADE_MAX, GRADE_MIN, Misconception, Strength, TopicType


class Chunk(BaseModel):
    """一条课标条目。编号在切分阶段就绑定，不交给模型 —— 见设计文档 §3.1。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    text: str
    standard_code: str
    source_file: str
    ordinal: int


class DraftContent(BaseModel):
    """**这个类就是给模型的 input_schema。**

    只放模型有资格产出的字段（内容判断类）。id / provenance / standard_codes
    一概不在这里 —— 自己声明自己可信是没有意义的。
    """

    model_config = ConfigDict(extra="forbid")

    name: str
    description: str
    type: TopicType
    subject: str
    domain: str
    grade_start: int = Field(ge=GRADE_MIN, le=GRADE_MAX)
    grade_end: int = Field(ge=GRADE_MIN, le=GRADE_MAX)
    evidence: list[str] = Field(min_length=1)
    assessment_prompt: str
    misconceptions: list[Misconception] = Field(default_factory=list)
    source_span: str = Field(description="抽自原文哪一句，供审核层复核")


class DraftBatch(BaseModel):
    """一次抽取调用的返回。工具的 input_schema 必须是对象，故包一层。"""

    model_config = ConfigDict(extra="forbid")

    drafts: list[DraftContent] = Field(default_factory=list)


class TopicDraft(BaseModel):
    """模型产出 + 流水线补齐的字段。"""

    model_config = ConfigDict(extra="forbid")

    draft_id: str
    chunk_id: str
    standard_codes: list[str] = Field(default_factory=list)
    content: DraftContent


class ProposedEdge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    prerequisite_draft_id: str
    strength: Strength
    reason: str = Field(min_length=1)


class ProposedEdgeBatch(BaseModel):
    model_config = ConfigDict(extra="forbid")

    edges: list[ProposedEdge] = Field(default_factory=list)


class TargetedEdge(BaseModel):
    """落盘用：ProposedEdge 只记前置，不记它属于谁 —— 落到文件里就读不出来了。"""

    model_config = ConfigDict(extra="forbid")

    target_draft_id: str
    edge: ProposedEdge


class Vote(BaseModel):
    model_config = ConfigDict(extra="forbid")

    reviewer: str
    approved: bool
    reason: str = ""


class ReviewOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    aspect: str
    votes: list[Vote]
    approved: bool


class DropRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    stage: str
    ref: str
    reason: str
    detail: str = ""


class Merge(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kept_draft_id: str
    dropped_draft_id: str
    reason: str = ""
