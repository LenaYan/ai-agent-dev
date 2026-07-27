"""生成流水线的内部类型。

与 `cn_curriculum_graph.models`（对外 schema）分开：这些类型只活在管道内部，
assemble 之后就丢掉。分开的好处是能把"字段归属"做成结构上的约束而非约定 ——
`DraftContent` 就是给模型的 input_schema，它装不下代码该填的字段。
"""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict, Field

from cn_curriculum_graph.models import GRADE_MAX, GRADE_MIN, Misconception, Strength, TopicType

# 这四类异常几乎总是程序 bug（属性拼错、类型不对、变量未定义、字典键缺失），
# 不该被各层"单条失败不中断整批"的 except Exception 悄悄吞掉、伪装成
# "这条 API 调用失败了" —— 那会把真实缺陷藏进 dropped.json，永远没人发现。
# 各层的批处理循环应先 `except PROGRAMMING_ERRORS: raise` 再 `except Exception`。
PROGRAMMING_ERRORS: tuple[type[Exception], ...] = (AttributeError, TypeError, NameError, KeyError)


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
    """一票。`approved` 是后果，`judgment` 是原始档位。

    **两者分开的理由**：三档判定（fidelity 的 faithful/reasonable_elaboration/
    fabricated、name_desc 的 consistent/scope_mismatch/topic_mismatch）映射到
    两级后果时会丢信息 —— `faithful` 与 `reasonable_elaboration` 都是
    `approved=True`，但"这个描述比原文具体"这件事必须能被程序读出来，
    而不是靠解析 `reason` 字符串。留痕要留成字段，不是留成文本。

    二值判定器（边审核）不填 `judgment`，保持 None。
    """

    model_config = ConfigDict(extra="forbid")

    reviewer: str
    approved: bool
    reason: str = ""
    judgment: str | None = None


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
