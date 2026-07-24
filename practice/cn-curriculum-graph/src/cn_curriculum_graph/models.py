"""中国课标知识依赖图 —— 数据模型（即 schema）。

与 Marble Skill Taxonomy 的四处刻意差异，见 docs/schema-design.md：
1. `misconceptions` —— 典型误概念，诊断类应用的核心字段，Marble 完全没有
2. `revisits`      —— 螺旋上升边，建模"同一概念多轮次递进"，扁平 DAG 表达不了
3. `provenance`    —— 每节点独立记录生成方式与审核状态，Marble 只有仓库级授权说明
4. `standards` + `textbook_units` —— 双层对齐：课标条目 + 教材单元（中国用户认教材）

年级（grade）而非年龄（age）作为进度基元：中国课标按年级组织，
1-6 为小学，7-9 为初中。这与 Marble 的 ageRange 是本土化的关键区别。
"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

TopicType = Literal[
    "conceptual",      # 概念理解，如"分数表示等分"
    "procedural",      # 程序技能，如"竖式除法"
    "representational",  # 表征转换，如"数线上标分数"
    "language",        # 术语语言，如"被除数/除数/商"
    "meta",            # 元认知，如"估算检验答案合理性"
]

Strength = Literal["hard", "soft"]

ReviewStatus = Literal[
    "unreviewed",       # 仅机器生成，未经人工审核
    "self_reviewed",    # 作者自审
    "expert_reviewed",  # 教研专业人员审核
]

GRADE_MIN = 1
GRADE_MAX = 9


class Misconception(BaseModel):
    """典型误概念。

    这是本 schema 相对 Marble 最有价值的增量：有了它，LLM 才能从
    "孩子答错了"推进到"孩子为什么这么想"。
    """

    model_config = ConfigDict(extra="forbid")

    statement: str = Field(description="孩子会怎么想，用孩子的口吻，如「0.45 比 0.405 大，因为 45 比 405 小」")
    probe: str = Field(description="能诱发这个错误的提问，用于确认是否真的存在该误概念")
    correction_hint: str = Field(description="纠正的切入点，不是标准答案，是该往哪儿引")


class Standard(BaseModel):
    """课标条目对齐 —— 只存编号，不存原文。

    与 Marble 对 NGSS / C3 / IB PYP 的 codes-only 策略一致。
    法律依据与不确定性见 docs/feasibility-analysis.md 第二节。
    """

    model_config = ConfigDict(extra="forbid")

    curriculum: str = Field(description="课标标识，如 cn-moe-math-2022")
    code: str = Field(description="条目编号，如 3.1.2")


class TextbookUnit(BaseModel):
    """教材单元对齐 —— 第二层坐标。

    只存版本+册次+单元号+单元名（事实性描述），不含教材正文、例题、习题。
    """

    model_config = ConfigDict(extra="forbid")

    edition: str = Field(description="版本，如 人教版")
    volume: str = Field(description="册次，如 四年级下册")
    unit: str = Field(description="单元号，如 4")
    title: str = Field(description="单元名，如 小数的意义和性质")


class Provenance(BaseModel):
    """每节点的来源与审核状态。

    Marble 最大的信任缺口就是没有这个 —— 其 PROVENANCE.md 通篇讲授权、
    不讲生产方法，导致数据可信度无从判断。这里从第一天就记。
    """

    model_config = ConfigDict(extra="forbid")

    method: str = Field(description="生成方式，如 llm-extract/gpt-x、hand-authored")
    review_status: ReviewStatus = "unreviewed"
    reviewer: str | None = None
    reviewed_at: str | None = Field(default=None, description="ISO 日期 YYYY-MM-DD")
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)


class Topic(BaseModel):
    """一个 micro-topic：一个可教、可评的最小知识单元。"""

    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    description: str
    type: TopicType
    subject: str
    domain: str
    grade_start: int = Field(ge=GRADE_MIN, le=GRADE_MAX)
    grade_end: int = Field(ge=GRADE_MIN, le=GRADE_MAX)
    evidence: list[str] = Field(min_length=1, description="掌握判据，可观察可验证")
    assessment_prompt: str = Field(description="面向家长/教师的一句话口头评测提问")
    misconceptions: list[Misconception] = Field(default_factory=list)
    standards: list[Standard] = Field(default_factory=list)
    textbook_units: list[TextbookUnit] = Field(default_factory=list)
    provenance: Provenance | None = None

    @model_validator(mode="after")
    def _grade_range_ordered(self) -> "Topic":
        if self.grade_end < self.grade_start:
            raise ValueError(
                f"topic {self.id}: grade_end({self.grade_end}) < grade_start({self.grade_start})"
            )
        return self


class Dependency(BaseModel):
    """先修边：掌握 topic_id 之前必须（hard）/最好（soft）先掌握 prerequisite_id。"""

    model_config = ConfigDict(extra="forbid")

    topic_id: str
    prerequisite_id: str
    strength: Strength
    reason: str = Field(min_length=1, description="一句话说明为什么是前置，供 LLM 直接用于讲解")

    @model_validator(mode="after")
    def _no_self_loop(self) -> "Dependency":
        if self.topic_id == self.prerequisite_id:
            raise ValueError(f"dependency 自环: {self.topic_id}")
        return self


class Revisit(BaseModel):
    """螺旋上升边：同一概念在更高年级的再次出现。

    例：分数在三上（初步认识）→ 五下（意义与性质）→ 六上（分数除法）。
    这不是先修关系（它本来就是同一个概念），扁平 DAG 表达不了这件事。
    """

    model_config = ConfigDict(extra="forbid")

    earlier_id: str
    later_id: str
    note: str = Field(min_length=1, description="这一轮相对上一轮深化了什么")

    @model_validator(mode="after")
    def _no_self_loop(self) -> "Revisit":
        if self.earlier_id == self.later_id:
            raise ValueError(f"revisit 自环: {self.earlier_id}")
        return self


class CurriculumGraph(BaseModel):
    """整张图。"""

    model_config = ConfigDict(extra="forbid")

    topics: list[Topic] = Field(default_factory=list)
    dependencies: list[Dependency] = Field(default_factory=list)
    revisits: list[Revisit] = Field(default_factory=list)

    @property
    def by_id(self) -> dict[str, Topic]:
        return {t.id: t for t in self.topics}
