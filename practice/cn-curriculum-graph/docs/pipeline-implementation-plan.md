# 生成流水线实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 从一段课标原文跑出一份能通过 `ccg-validate` 且 0 error 的 `graph.json`，每层落盘可重入。

**Architecture:** 纯 DAG 手写工作流，六层各为 `(input, deps) -> output` 的纯函数，LLM 客户端依赖注入。六层里 chunk / assemble 是纯规则，dedupe 是规则配对 + LLM 确认，其余为 LLM。无框架、无反馈环、无并发。

**Tech Stack:** Python 3.12、pydantic v2、anthropic SDK（指向 DeepSeek 的 Anthropic 兼容端点）、pytest、uv。

设计依据：`docs/pipeline-design.md`。所有"为什么这么设计"的问题去那里找答案，本文件只讲怎么做。

## Global Constraints

- 工作目录一律 `practice/cn-curriculum-graph/`，命令用 `uv run`
- 全程 TDD：先写失败测试 → 跑一次看到 RED → 最小实现 → 跑到 GREEN → 提交。**不许先写实现**
- 所有 pydantic 模型带 `model_config = ConfigDict(extra="forbid")`
- LLM 客户端一律构造函数依赖注入，`import anthropic` 懒加载；单测零网络零 key
- 结构化输出一律走**强制工具调用**（`tool_choice={"type":"tool","name":...}` + `thinking={"type":"disabled"}` + `temperature=0`），不用 `output_format`
- DeepSeek 端点常量复用 `judges.deepseek_judge.DEEPSEEK_BASE_URL`，key 读 `DEEPSEEK_API_KEY`，**绝不读写 `ANTHROPIC_BASE_URL`**
- 每层丢弃的条目必须写 `DropRecord`，没有静默跳过
- 注释与文档字符串用中文，术语保留英文原词
- 每个 Task 结束时全量 `uv run pytest` 必须绿

---

## File Structure

| 文件 | 职责 |
|---|---|
| `src/cn_curriculum_graph/pipeline/models.py` | 流水线内部类型：Chunk / DraftContent / TopicDraft / Vote / ReviewOutcome / DropRecord / Merge |
| `src/cn_curriculum_graph/pipeline/io.py` | 分层落盘读写与 drop 追加 |
| `src/cn_curriculum_graph/pipeline/chunk.py` | 纯规则切分 |
| `src/cn_curriculum_graph/pipeline/extract.py` | Extractor 协议 + DeepSeek 实现 |
| `src/cn_curriculum_graph/pipeline/dedupe.py` | 候选配对、同源判定、合并、同名不同义处理 |
| `src/cn_curriculum_graph/pipeline/edges.py` | 剪枝 + EdgeProposer 协议与实现 |
| `src/cn_curriculum_graph/pipeline/review.py` | 三个审核维度 + 投票 |
| `src/cn_curriculum_graph/pipeline/assemble.py` | id 生成、provenance 填充、组装成 CurriculumGraph |
| `src/cn_curriculum_graph/pipeline/run.py` | 编排、`--from` 重入、CLI 入口 |
| `tests/pipeline/test_*.py` | 每层一个测试文件 + 一个端到端 |

---

### Task 1: 流水线数据模型与落盘

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/__init__.py`（空）
- Create: `src/cn_curriculum_graph/pipeline/models.py`
- Create: `src/cn_curriculum_graph/pipeline/io.py`
- Create: `tests/pipeline/__init__.py`（空）
- Test: `tests/pipeline/test_models.py`, `tests/pipeline/test_io.py`

**Interfaces:**
- Consumes: `cn_curriculum_graph.models.{Misconception, TopicType, Strength}`
- Produces: `Chunk`, `DraftContent`, `TopicDraft`, `ProposedEdge`, `Vote`, `ReviewOutcome`, `DropRecord`, `Merge`；`io.write_stage/read_stage/append_drops`

- [ ] **Step 1: 写失败测试 —— 模型的字段归属边界**

创建 `tests/pipeline/__init__.py`（空文件）和 `tests/pipeline/test_models.py`：

```python
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
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_models.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline'`

- [ ] **Step 3: 实现模型**

创建 `src/cn_curriculum_graph/pipeline/__init__.py`（空文件），然后 `src/cn_curriculum_graph/pipeline/models.py`：

```python
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
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_models.py -q
```
Expected: PASS，6 passed

- [ ] **Step 5: 写落盘的失败测试**

创建 `tests/pipeline/test_io.py`：

```python
"""分层落盘：每层产物可单独重跑、可人眼检查。"""

from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline.models import Chunk, DropRecord


def _chunk(ordinal: int) -> Chunk:
    return Chunk(
        id=f"src#{ordinal:03d}",
        text="能理解小数的意义",
        standard_code="3.1.2",
        source_file="src.md",
        ordinal=ordinal,
    )


def test_round_trips_a_stage_file(tmp_path):
    path = tmp_path / "01-chunks.json"
    io.write_stage(path, [_chunk(1), _chunk(2)])

    loaded = io.read_stage(path, Chunk)

    assert [c.id for c in loaded] == ["src#001", "src#002"]


def test_stage_file_is_human_readable_utf8(tmp_path):
    path = tmp_path / "01-chunks.json"
    io.write_stage(path, [_chunk(1)])

    # 中文不能被转义成 \uXXXX —— 这些文件是给人看的
    assert "能理解小数的意义" in path.read_text(encoding="utf-8")


def test_append_drops_accumulates_across_stages(tmp_path):
    path = tmp_path / "dropped.json"
    io.append_drops(path, [DropRecord(stage="chunk", ref="a", reason="NO_STANDARD_CODE")])
    io.append_drops(path, [DropRecord(stage="review", ref="b", reason="VOTE_SPLIT")])

    loaded = io.read_stage(path, DropRecord)

    assert [d.stage for d in loaded] == ["chunk", "review"]


def test_read_stage_returns_empty_for_missing_file(tmp_path):
    assert io.read_stage(tmp_path / "nope.json", DropRecord) == []
```

- [ ] **Step 6: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_io.py -q
```
Expected: FAIL，`ImportError: cannot import name 'io'`

- [ ] **Step 7: 实现落盘**

创建 `src/cn_curriculum_graph/pipeline/io.py`：

```python
"""分层落盘。

每层产物单独成文件，目的有两个：可从任意层重入（`--from`），
以及**可人眼检查**。第二条是 effective-agents 心法③"像你的 Agent 一样思考"的
直接落实 —— 看不见中间状态就没法判断它到底在干什么。故 JSON 一律
ensure_ascii=False + indent=2，宁可文件大一点也要能读。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)


def write_stage(path: Path, items: list[BaseModel]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = [item.model_dump(mode="json") for item in items]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def read_stage(path: Path, model: type[T]) -> list[T]:
    """文件不存在时返回空列表 —— 上一层可能一条都没产出，这不是错误。"""
    if not path.exists():
        return []
    return [model.model_validate(raw) for raw in json.loads(path.read_text(encoding="utf-8"))]


def append_drops(path: Path, records: list[BaseModel]) -> None:
    """dropped.json 是跨层累加的，不是每层覆盖。"""
    if not records:
        return
    existing = json.loads(path.read_text(encoding="utf-8")) if path.exists() else []
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = existing + [r.model_dump(mode="json") for r in records]
    path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
```

- [ ] **Step 8: 跑全量测试**

```bash
uv run pytest -q
```
Expected: PASS，62 passed（原 58 + 新增 4... 实际为 58 + 6 + 4 = 68）

- [ ] **Step 9: 提交**

```bash
git add src/cn_curriculum_graph/pipeline tests/pipeline
git commit -m "feat(pipeline): 流水线内部类型与分层落盘

DraftContent 即给模型的 input_schema，结构上装不下 id/provenance/
standard_codes —— 把字段归属做成约束而非约定。"
```

---

### Task 2: chunk 层（纯规则切分）

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/chunk.py`
- Test: `tests/pipeline/test_chunk.py`

**Interfaces:**
- Consumes: `models.{Chunk, DropRecord}`
- Produces: `chunk.split_source(text: str, source_file: str) -> tuple[list[Chunk], list[DropRecord]]`

**输入格式契约**：段落以空行分隔；每段首行以形如 `3.1.2` 的条目编号开头（两级以上，点分数字）。不带编号的段落丢弃，原因码 `NO_STANDARD_CODE`。

- [ ] **Step 1: 写失败测试**

创建 `tests/pipeline/test_chunk.py`：

```python
"""切分是纯规则层。

编号必须在这一层绑定：校验层的 LOW_STANDARDS_COVERAGE 是带硬阈值的 ERROR，
抽不到编号足以让整批产出被自己的 CI 拒掉。而编号在原文里是有格式的，
纯规则就能拿，交给模型只会平白引入不确定性。
"""

from cn_curriculum_graph.pipeline.chunk import split_source

SOURCE = """3.1.2 能理解小数的意义，会比较小数的大小。

3.1.3 能进行简单的小数加减运算。
会解决相关的简单实际问题。

这一段没有条目编号，属于导言。

4.2.1 能认识常见的平面图形。
"""


def test_splits_paragraphs_into_numbered_chunks():
    chunks, _ = split_source(SOURCE, source_file="math.md")

    assert [c.standard_code for c in chunks] == ["3.1.2", "3.1.3", "4.2.1"]


def test_chunk_ids_are_deterministic_and_ordered():
    chunks, _ = split_source(SOURCE, source_file="math.md")

    assert [c.id for c in chunks] == ["math#001", "math#002", "math#003"]
    assert [c.ordinal for c in chunks] == [1, 2, 3]


def test_chunk_text_keeps_the_whole_paragraph_without_the_code():
    chunks, _ = split_source(SOURCE, source_file="math.md")

    second = chunks[1]
    assert second.text.startswith("能进行简单的小数加减运算")
    assert "会解决相关的简单实际问题" in second.text
    assert not second.text.startswith("3.1.3")


def test_paragraph_without_a_code_is_dropped_with_a_reason():
    _, drops = split_source(SOURCE, source_file="math.md")

    assert len(drops) == 1
    assert drops[0].stage == "chunk"
    assert drops[0].reason == "NO_STANDARD_CODE"
    assert "导言" in drops[0].detail


def test_single_level_number_is_not_a_standard_code():
    """『1. 前言』这种列表序号不是条目编号，要求至少两级。"""
    chunks, drops = split_source("1. 前言部分。", source_file="math.md")

    assert chunks == []
    assert drops[0].reason == "NO_STANDARD_CODE"


def test_empty_source_yields_nothing():
    assert split_source("", source_file="math.md") == ([], [])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_chunk.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.chunk'`

- [ ] **Step 3: 实现切分**

创建 `src/cn_curriculum_graph/pipeline/chunk.py`：

```python
"""切分课标文本为带条目编号的片段 —— 纯规则，不需要模型。

输入契约：段落以空行分隔，每段首行以形如 `3.1.2` 的条目编号开头
（至少两级，点分数字；单级如"1."是列表序号，不算）。

切不出编号的段落直接丢弃并记 NO_STANDARD_CODE。这通常意味着切分规则
与该份素材的排版不匹配，是需要人看的信号，不该带病往下走。
"""

from __future__ import annotations

import re

from cn_curriculum_graph.pipeline.models import Chunk, DropRecord

# 至少两级的点分数字，如 3.1.2 / 4.2；后跟空白或全角空格
_CODE = re.compile(r"^\s*(\d+(?:\.\d+)+)[\s　]+(.*)", re.DOTALL)


def split_source(text: str, source_file: str) -> tuple[list[Chunk], list[DropRecord]]:
    stem = source_file.rsplit(".", 1)[0]
    chunks: list[Chunk] = []
    drops: list[DropRecord] = []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for para in paragraphs:
        matched = _CODE.match(para)
        if matched is None:
            drops.append(
                DropRecord(
                    stage="chunk",
                    ref=f"{stem}:{len(chunks) + len(drops) + 1}",
                    reason="NO_STANDARD_CODE",
                    detail=para[:60],
                )
            )
            continue
        code, body = matched.group(1), matched.group(2).strip()
        ordinal = len(chunks) + 1
        chunks.append(
            Chunk(
                id=f"{stem}#{ordinal:03d}",
                text=body,
                standard_code=code,
                source_file=source_file,
                ordinal=ordinal,
            )
        )

    return chunks, drops
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_chunk.py -q
```
Expected: PASS，6 passed

- [ ] **Step 5: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/chunk.py tests/pipeline/test_chunk.py
git commit -m "feat(pipeline): chunk 层，条目编号在切分时绑定

编号交给模型会拖低 standards 对齐率，而 LOW_STANDARDS_COVERAGE
是带硬阈值的 ERROR —— 足以让整批产出被自己的 CI 拒掉。"
```

---

### Task 3: extract 层（LLM 抽取）

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/extract.py`
- Test: `tests/pipeline/test_extract.py`

**Interfaces:**
- Consumes: `models.{Chunk, DraftBatch, DraftContent, TopicDraft, DropRecord}`；`judges.deepseek_judge.DEEPSEEK_BASE_URL`
- Produces: `Extractor`（Protocol，`__call__(chunk: Chunk) -> DraftBatch`）、`DeepSeekExtractor`、`extract_all(chunks, extractor) -> tuple[list[TopicDraft], list[DropRecord]]`、`EXTRACT_TOOL_NAME`

- [ ] **Step 1: 写失败测试**

创建 `tests/pipeline/test_extract.py`：

```python
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
    recorder: dict = {}
    client = SimpleNamespace(
        messages=SimpleNamespace(
            create=lambda **kw: SimpleNamespace(
                content=[SimpleNamespace(type="text", text="我不知道")]
            )
        )
    )

    with pytest.raises(ValueError, match=EXTRACT_TOOL_NAME):
        DeepSeekExtractor(client=client)(_chunk())
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_extract.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.extract'`

- [ ] **Step 3: 实现抽取层**

创建 `src/cn_curriculum_graph/pipeline/extract.py`：

```python
"""从课标条目抽取候选知识点。

结构化输出走**强制工具调用**而非原生 output_format：DeepSeek 的兼容端点
照收 output_format 却不遵守（实测返回自由文本），强制工具调用才可移植。
工具的 input_schema 就是 DraftBatch —— 也就是说，模型能填什么字段，
由类型系统而非提示词约束。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import Chunk, DraftBatch, DropRecord, TopicDraft

DEFAULT_MODEL = "deepseek-v4-flash"
EXTRACT_TOOL_NAME = "record_topics"

_SYSTEM = (
    "你是小学课标知识依赖图的构建者。"
    "会给你一条课程标准条目的正文，请把它拆成可教、可评的知识点（micro-topic）。\n"
    "一条条目可能对应一个知识点，也可能对应多个；如果拆不出任何可教的知识点，返回空列表。\n"
    "每个知识点要求：\n"
    "- name：简短的知识点名称，必须能概括 description 的主要内容\n"
    "- description：这个知识点具体教什么，一到两句\n"
    "- evidence：至少一条可观察可验证的掌握判据，写成能直接拿去考查的样子\n"
    "- assessment_prompt：一句面向家长或老师的口头提问\n"
    "- misconceptions：孩子典型的想错方式，没有把握就留空，不要编\n"
    "- source_span：**原文中支撑这个知识点的那一句**，必须逐字来自给你的正文\n"
    "- grade_start/grade_end：中国义务教育年级，1-9\n"
    "只依据给你的正文，不要引入正文之外的内容。"
)

_TOOL = {
    "name": EXTRACT_TOOL_NAME,
    "description": "记录从这条课标条目抽出的全部知识点",
    "input_schema": DraftBatch.model_json_schema(),
}


class Extractor(Protocol):
    def __call__(self, chunk: Chunk) -> DraftBatch: ...


class DeepSeekExtractor:
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model

    def __call__(self, chunk: Chunk) -> DraftBatch:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=4096,
            temperature=0,
            system=_SYSTEM,
            messages=[{"role": "user", "content": f"课标条目正文：\n{chunk.text}"}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": EXTRACT_TOOL_NAME},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == EXTRACT_TOOL_NAME:
                # 强制 tool_choice 只保证"调了工具"，不保证参数合法
                return DraftBatch.model_validate(block.input)
        raise ValueError(f"模型未调用 {EXTRACT_TOOL_NAME} 工具，返回：{response.content!r}")


def extract_all(
    chunks: list[Chunk], extractor: Extractor
) -> tuple[list[TopicDraft], list[DropRecord]]:
    """逐 chunk 抽取。单个 chunk 失败不中断整批 —— 记账后继续。"""
    drafts: list[TopicDraft] = []
    drops: list[DropRecord] = []

    for chunk in chunks:
        try:
            batch = extractor(chunk)
        except Exception as exc:  # noqa: BLE001 —— 任何失败都只影响这一条
            drops.append(
                DropRecord(
                    stage="extract",
                    ref=chunk.id,
                    reason="EXTRACT_FAILED",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        if not batch.drafts:
            drops.append(
                DropRecord(
                    stage="extract", ref=chunk.id, reason="NO_DRAFTS", detail=chunk.text[:60]
                )
            )
            continue

        for index, content in enumerate(batch.drafts):
            drafts.append(
                TopicDraft(
                    draft_id=f"{chunk.id}-{index}",
                    chunk_id=chunk.id,
                    standard_codes=[chunk.standard_code],
                    content=content,
                )
            )

    return drafts, drops
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_extract.py -q
```
Expected: PASS，6 passed

- [ ] **Step 5: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/extract.py tests/pipeline/test_extract.py
git commit -m "feat(pipeline): extract 层，强制工具调用取结构化输出

工具 input_schema 即 DraftBatch —— 模型能填什么字段由类型系统约束，
不靠提示词自觉。单 chunk 失败记账后继续，不中断整批。"
```

---

### Task 4: dedupe 层（规则配对 + LLM 确认 + 合并）

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/dedupe.py`
- Test: `tests/pipeline/test_dedupe.py`

**Interfaces:**
- Consumes: `models.{TopicDraft, DraftContent, DropRecord, Merge}`
- Produces: `normalize_name(str) -> str`、`candidate_pairs(list[TopicDraft]) -> list[tuple[int, int]]`、`SameTopicVerdict`、`SameTopicJudge`（Protocol）、`DeepSeekSameTopicJudge`、`SAME_TOPIC_TOOL_NAME`、`dedupe(drafts, judge) -> DedupeResult(kept, merges, drops)`

- [ ] **Step 1: 写归一化与配对的失败测试**

创建 `tests/pipeline/test_dedupe.py`：

```python
"""去重层：规则负责缩小范围（宁可多给候选），LLM 负责下判断。

同名不同义必须显式处理 —— 实测 Marble 全量 1590 节点，28 条 NAME_DESC_MISMATCH
里 86% 落在跨年龄段复用的同名节点上。那是结构性缺陷，不是随机噪声。
"""

from types import SimpleNamespace

from cn_curriculum_graph.pipeline.dedupe import (
    SAME_TOPIC_TOOL_NAME,
    DeepSeekSameTopicJudge,
    SameTopicVerdict,
    candidate_pairs,
    dedupe,
    normalize_name,
)
from cn_curriculum_graph.pipeline.models import DraftContent, TopicDraft


def _draft(draft_id: str, name: str, *, grade: int = 4, evidence=None, desc: str = "描述") -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id=draft_id.split("-")[0],
        standard_codes=[f"3.1.{draft_id[-1]}"],
        content=DraftContent(
            name=name,
            description=desc,
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=grade,
            grade_end=grade,
            evidence=evidence or ["证据一"],
            assessment_prompt="问一句",
            source_span="原文",
        ),
    )


def test_normalize_strips_whitespace_punctuation_and_case():
    assert normalize_name(" 小数的  意义、 ") == normalize_name("小数的意义")
    assert normalize_name("Decimal Place Value") == normalize_name("decimalplacevalue")


def test_normalize_folds_fullwidth_to_halfwidth():
    assert normalize_name("小数（一）") == normalize_name("小数(一)")


def test_pairs_drafts_with_identical_normalized_names():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", " 小数的意义 "), _draft("c-3", "分数")]

    assert candidate_pairs(drafts) == [(0, 1)]


def test_pairs_drafts_with_similar_names_above_threshold():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", "小数的意义与性质")]

    assert candidate_pairs(drafts) == [(0, 1)]


def test_pairs_drafts_sharing_a_standard_code():
    left, right = _draft("a-1", "甲概念"), _draft("b-1", "完全不同的乙")
    right.standard_codes = list(left.standard_codes)

    assert candidate_pairs([left, right]) == [(0, 1)]


def test_unrelated_drafts_are_not_paired():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", "三角形内角和")]

    assert candidate_pairs(drafts) == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_dedupe.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.dedupe'`

- [ ] **Step 3: 实现归一化与配对**

创建 `src/cn_curriculum_graph/pipeline/dedupe.py`：

```python
"""去重：同一知识点常被多条课标条目提到。

分工刻意如此 —— **规则只负责缩小范围，宁可多给候选**，判断交给 LLM。
反过来（规则直接判同一）会把"小数的意义"和"小数的意义与性质"这种
需要语义才能分辨的情形判错。
"""

from __future__ import annotations

import difflib
import os
import re
import unicodedata
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import DropRecord, Merge, TopicDraft

DEFAULT_MODEL = "deepseek-v4-flash"
SAME_TOPIC_TOOL_NAME = "record_same_topic"
SIMILARITY_THRESHOLD = 0.85

_PUNCT = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize_name(name: str) -> str:
    """去空白、去标点、转小写、全角转半角。只用于配对，不改原始 name。"""
    folded = unicodedata.normalize("NFKC", name).lower()
    return _PUNCT.sub("", folded)


def candidate_pairs(drafts: list[TopicDraft]) -> list[tuple[int, int]]:
    """满足任一即进候选：归一化名相同、相似度 ≥ 阈值、条目编号有交集。"""
    pairs: list[tuple[int, int]] = []
    names = [normalize_name(d.content.name) for d in drafts]

    for i in range(len(drafts)):
        for j in range(i + 1, len(drafts)):
            same_name = names[i] == names[j]
            similar = (
                difflib.SequenceMatcher(None, names[i], names[j]).ratio() >= SIMILARITY_THRESHOLD
            )
            shared_code = bool(set(drafts[i].standard_codes) & set(drafts[j].standard_codes))
            if same_name or similar or shared_code:
                pairs.append((i, j))

    return pairs
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_dedupe.py -q
```
Expected: PASS，6 passed

- [ ] **Step 5: 写合并与同名处理的失败测试**

追加到 `tests/pipeline/test_dedupe.py`：

```python
def _judge(same_ids: set[frozenset[str]]):
    def judge(a: TopicDraft, b: TopicDraft) -> SameTopicVerdict:
        key = frozenset({a.draft_id, b.draft_id})
        return SameTopicVerdict(same=key in same_ids, reason="测试判定")

    return judge


def test_merges_take_the_draft_with_more_evidence_as_the_base():
    thin = _draft("a-1", "小数的意义", evidence=["证据一"])
    rich = _draft("b-2", "小数的意义", evidence=["证据一", "证据二"])

    result = dedupe([thin, rich], _judge({frozenset({"a-1", "b-2"})}))

    assert [d.draft_id for d in result.kept] == ["b-2"]
    assert result.merges[0].kept_draft_id == "b-2"
    assert result.merges[0].dropped_draft_id == "a-1"


def test_merge_unions_evidence_and_standard_codes():
    left = _draft("a-1", "小数的意义", evidence=["证据甲"])
    right = _draft("b-2", "小数的意义", evidence=["证据乙"])

    result = dedupe([left, right], _judge({frozenset({"a-1", "b-2"})}))

    kept = result.kept[0]
    assert set(kept.content.evidence) == {"证据甲", "证据乙"}
    assert set(kept.standard_codes) == {"3.1.1", "3.1.2"}


def test_ties_break_deterministically_by_description_then_id():
    short = _draft("b-2", "小数的意义", desc="短")
    long_desc = _draft("a-1", "小数的意义", desc="长得多的一段描述")

    result = dedupe([short, long_desc], _judge({frozenset({"a-1", "b-2"})}))

    assert [d.draft_id for d in result.kept] == ["a-1"]


def test_same_name_different_topic_gets_a_grade_qualifier():
    """同名不同义不许共存 —— 加年级限定词区分，这是 Marble 那 86% 的成因。"""
    early = _draft("a-1", "认识角", grade=4)
    late = _draft("b-2", "认识角", grade=7)

    result = dedupe([early, late], _judge(set()))  # 判为不同

    names = sorted(d.content.name for d in result.kept)
    assert names == ["认识角（4年级）", "认识角（7年级）"]


def test_same_name_same_grade_but_different_topic_drops_the_later_one():
    """加了年级还撞名，说明真的无法自动区分 —— 丢弃并记账，交给人看。"""
    first = _draft("a-1", "认识角", grade=4)
    second = _draft("b-2", "认识角", grade=4)

    result = dedupe([first, second], _judge(set()))

    assert [d.draft_id for d in result.kept] == ["a-1"]
    assert result.drops[0].reason == "SAME_NAME_DIFFERENT_TOPIC"
    assert result.drops[0].ref == "b-2"


def test_unrelated_drafts_pass_through_untouched():
    drafts = [_draft("a-1", "小数的意义"), _draft("b-2", "三角形内角和")]

    result = dedupe(drafts, _judge(set()))

    assert [d.draft_id for d in result.kept] == ["a-1", "b-2"]
    assert result.merges == []
    assert result.drops == []


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", name=SAME_TOPIC_TOOL_NAME, input=tool_input)
            ]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_same_topic_judge_forces_its_tool():
    recorder: dict = {}
    client = _fake_client(recorder, {"same": True, "reason": "都是小数的意义"})

    verdict = DeepSeekSameTopicJudge(client=client)(
        _draft("a-1", "小数的意义"), _draft("b-2", "小数含义")
    )

    assert verdict.same is True
    assert recorder["tool_choice"] == {"type": "tool", "name": SAME_TOPIC_TOOL_NAME}
    assert recorder["thinking"] == {"type": "disabled"}
    assert recorder["temperature"] == 0
```

- [ ] **Step 6: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_dedupe.py -q
```
Expected: FAIL，`ImportError: cannot import name 'dedupe'`

- [ ] **Step 7: 实现合并、同名处理与 LLM 判定器**

追加到 `src/cn_curriculum_graph/pipeline/dedupe.py`：

```python
class SameTopicVerdict(BaseModel):
    model_config = ConfigDict(extra="forbid")

    same: bool
    reason: str = ""


class SameTopicJudge(Protocol):
    def __call__(self, a: TopicDraft, b: TopicDraft) -> SameTopicVerdict: ...


class DedupeResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kept: list[TopicDraft]
    merges: list[Merge]
    drops: list[DropRecord]


_SAME_TOPIC_SYSTEM = (
    "你是小学课标知识依赖图的数据质检员。"
    "会给你两个候选知识点的名称与描述，判断它们讲的是不是**同一个知识点**。\n"
    "判 same=true：两者教的是同一件事，只是措辞、详略或来源条目不同。\n"
    "判 same=false：两者教的技能或概念不同，即便名称相近。\n"
    "拿不准时判 false —— 错误合并会永久丢失一个知识点，错误保留只是多一个节点。\n"
    "reason 用一句中文说明依据。"
)

_SAME_TOPIC_TOOL = {
    "name": SAME_TOPIC_TOOL_NAME,
    "description": "记录两个候选知识点是否为同一个",
    "input_schema": SameTopicVerdict.model_json_schema(),
}


class DeepSeekSameTopicJudge:
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model

    def __call__(self, a: TopicDraft, b: TopicDraft) -> SameTopicVerdict:
        prompt = (
            f"知识点一\n名称：{a.content.name}\n描述：{a.content.description}\n\n"
            f"知识点二\n名称：{b.content.name}\n描述：{b.content.description}"
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=0,
            system=_SAME_TOPIC_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[_SAME_TOPIC_TOOL],
            tool_choice={"type": "tool", "name": SAME_TOPIC_TOOL_NAME},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == SAME_TOPIC_TOOL_NAME:
                return SameTopicVerdict.model_validate(block.input)
        raise ValueError(f"模型未调用 {SAME_TOPIC_TOOL_NAME} 工具，返回：{response.content!r}")


def _better_base(a: TopicDraft, b: TopicDraft) -> tuple[TopicDraft, TopicDraft]:
    """选合并基底：evidence 多者胜；并列取 description 长者；再并列取 draft_id 小者。"""
    key_a = (len(a.content.evidence), len(a.content.description))
    key_b = (len(b.content.evidence), len(b.content.description))
    if key_a > key_b or (key_a == key_b and a.draft_id <= b.draft_id):
        return a, b
    return b, a


def _merge_into(base: TopicDraft, other: TopicDraft) -> TopicDraft:
    seen_statements = {m.statement for m in base.content.misconceptions}
    merged = base.model_copy(deep=True)
    merged.content.evidence = list(dict.fromkeys(base.content.evidence + other.content.evidence))
    merged.content.misconceptions += [
        m for m in other.content.misconceptions if m.statement not in seen_statements
    ]
    merged.standard_codes = list(dict.fromkeys(base.standard_codes + other.standard_codes))
    return merged


def dedupe(drafts: list[TopicDraft], judge: SameTopicJudge) -> DedupeResult:
    by_id = {d.draft_id: d.model_copy(deep=True) for d in drafts}
    order = [d.draft_id for d in drafts]
    merges: list[Merge] = []
    drops: list[DropRecord] = []
    dropped: set[str] = set()

    for i, j in candidate_pairs(drafts):
        left_id, right_id = order[i], order[j]
        if left_id in dropped or right_id in dropped:
            continue

        verdict = judge(by_id[left_id], by_id[right_id])
        if verdict.same:
            base, other = _better_base(by_id[left_id], by_id[right_id])
            by_id[base.draft_id] = _merge_into(base, other)
            dropped.add(other.draft_id)
            merges.append(
                Merge(
                    kept_draft_id=base.draft_id,
                    dropped_draft_id=other.draft_id,
                    reason=verdict.reason,
                )
            )
            continue

        # 判为不同，但名字撞了 —— 不许共存，先试年级限定词
        left, right = by_id[left_id], by_id[right_id]
        if normalize_name(left.content.name) != normalize_name(right.content.name):
            continue
        if left.content.grade_start != right.content.grade_start:
            for draft in (left, right):
                draft.content.name = f"{draft.content.name}（{draft.content.grade_start}年级）"
            continue
        # 加了年级还撞名 —— 自动区分不了，丢后者交给人看
        dropped.add(right_id)
        drops.append(
            DropRecord(
                stage="dedupe",
                ref=right_id,
                reason="SAME_NAME_DIFFERENT_TOPIC",
                detail=f"与 {left_id} 同名同年级但判为不同知识点：{verdict.reason}",
            )
        )

    kept = [by_id[i] for i in order if i not in dropped]
    return DedupeResult(kept=kept, merges=merges, drops=drops)
```

- [ ] **Step 8: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_dedupe.py -q
```
Expected: PASS，14 passed

- [ ] **Step 9: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/dedupe.py tests/pipeline/test_dedupe.py
git commit -m "feat(pipeline): dedupe 层，规则配对 + LLM 确认 + 同名强制消歧

同名不同义先加年级限定词，仍撞名则丢弃记账 —— Marble 28 条 ERROR
里 86% 就是这个成因，不能让它在我们自己的产出里重演。"
```

---

### Task 5: edges 层（剪枝 + LLM 连边）

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/edges.py`
- Test: `tests/pipeline/test_edges.py`

**Interfaces:**
- Consumes: `models.{TopicDraft, ProposedEdge, ProposedEdgeBatch, DropRecord}`
- Produces: `MAX_GRADE_GAP`、`candidate_prerequisites(drafts) -> dict[str, list[TopicDraft]]`、`EdgeProposer`（Protocol）、`DeepSeekEdgeProposer`、`EDGE_TOOL_NAME`、`propose_all(drafts, proposer) -> tuple[dict[str, list[ProposedEdge]], list[DropRecord]]`

- [ ] **Step 1: 写剪枝的失败测试**

创建 `tests/pipeline/test_edges.py`：

```python
"""连边层。

剪枝规则**直接由校验规则反推**：GRADE_INVERSION 会拒绝"前置年级晚于后继"，
那就干脆不生成这类候选。生成端和校验端共用同一套约束，
省的不只是调用次数 —— 同一套约束写两遍才是 bug 的温床。
"""

from types import SimpleNamespace

import pytest

from cn_curriculum_graph.pipeline.edges import (
    EDGE_TOOL_NAME,
    MAX_GRADE_GAP,
    DeepSeekEdgeProposer,
    candidate_prerequisites,
    propose_all,
)
from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, ProposedEdgeBatch, TopicDraft


def _draft(draft_id: str, grade: int, name: str = "某知识点") -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id="c#001",
        standard_codes=["3.1.1"],
        content=DraftContent(
            name=name,
            description="描述",
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=grade,
            grade_end=grade,
            evidence=["证据"],
            assessment_prompt="问一句",
            source_span="原文",
        ),
    )


def test_max_grade_gap_is_two():
    assert MAX_GRADE_GAP == 2


def test_candidates_exclude_later_grades():
    early, late = _draft("a", grade=3), _draft("b", grade=5)

    candidates = candidate_prerequisites([early, late])

    assert [c.draft_id for c in candidates["b"]] == ["a"]
    assert candidates["a"] == []


def test_candidates_exclude_gaps_beyond_the_limit():
    early, far = _draft("a", grade=1), _draft("b", grade=5)

    assert candidate_prerequisites([early, far])["b"] == []


def test_same_grade_drafts_are_mutual_candidates():
    left, right = _draft("a", grade=4), _draft("b", grade=4)

    candidates = candidate_prerequisites([left, right])

    assert [c.draft_id for c in candidates["a"]] == ["b"]
    assert [c.draft_id for c in candidates["b"]] == ["a"]


def test_a_draft_is_never_its_own_candidate():
    only = _draft("a", grade=4)

    assert candidate_prerequisites([only])["a"] == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_edges.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.edges'`

- [ ] **Step 3: 实现剪枝**

创建 `src/cn_curriculum_graph/pipeline/edges.py`：

```python
"""生成先修依赖边。

朴素做法是两两配对，30 个节点就是 435 次调用。这里改成
**剪枝 + 每节点一次**：把该节点和它的全部候选前置一起给模型，
让它一次输出选中的边。调用次数从 N² 降到 N。

剪枝规则来自校验规则的反推，见 docs/pipeline-design.md §3.4。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import (
    DropRecord,
    ProposedEdge,
    ProposedEdgeBatch,
    TopicDraft,
)

DEFAULT_MODEL = "deepseek-v4-flash"
EDGE_TOOL_NAME = "record_prerequisites"
# 跨度超过两个年级的先修基本是间接的，砍掉让传递性去表达
MAX_GRADE_GAP = 2


def candidate_prerequisites(drafts: list[TopicDraft]) -> dict[str, list[TopicDraft]]:
    """候选前置：年级不晚于目标，且跨度不超过 MAX_GRADE_GAP。"""
    candidates: dict[str, list[TopicDraft]] = {}
    for target in drafts:
        candidates[target.draft_id] = [
            other
            for other in drafts
            if other.draft_id != target.draft_id
            and other.content.grade_start <= target.content.grade_start
            and target.content.grade_start - other.content.grade_start <= MAX_GRADE_GAP
        ]
    return candidates
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_edges.py -q
```
Expected: PASS，5 passed

- [ ] **Step 5: 写连边编排与判定器的失败测试**

追加到 `tests/pipeline/test_edges.py`：

```python
def test_proposes_edges_per_draft_and_keeps_only_known_prerequisites():
    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="先学 a")]
        )

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4)], proposer)

    assert [e.prerequisite_draft_id for e in edges["b"]] == ["a"]
    assert drops == []


def test_edges_pointing_at_unknown_drafts_are_dropped():
    """模型可能编一个不存在的 id 出来 —— 丢掉并记账，别让它污染图。"""

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[ProposedEdge(prerequisite_draft_id="不存在", strength="hard", reason="乱说")]
        )

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4)], proposer)

    assert edges["b"] == []
    assert drops[0].reason == "UNKNOWN_PREREQUISITE"
    assert "不存在" in drops[0].detail


def test_drafts_without_candidates_skip_the_model_entirely():
    calls = []

    def proposer(target, candidates):
        calls.append(target.draft_id)
        return ProposedEdgeBatch(edges=[])

    propose_all([_draft("a", 3)], proposer)

    # 没有候选就没有可问的，省一次调用
    assert calls == []


def test_a_failing_draft_is_dropped_without_stopping_the_batch():
    def proposer(target, candidates):
        raise RuntimeError("API 炸了")

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4)], proposer)

    assert edges["b"] == []
    assert drops[0].reason == "EDGES_FAILED"
    assert "API 炸了" in drops[0].detail


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=EDGE_TOOL_NAME, input=tool_input)]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_edge_proposer_forces_its_tool_and_shows_candidate_ids():
    recorder: dict = {}
    client = _fake_client(
        recorder,
        {"edges": [{"prerequisite_draft_id": "a", "strength": "hard", "reason": "先学 a"}]},
    )

    batch = DeepSeekEdgeProposer(client=client)(_draft("b", 4), [_draft("a", 3, name="甲")])

    assert [e.prerequisite_draft_id for e in batch.edges] == ["a"]
    assert recorder["tool_choice"] == {"type": "tool", "name": EDGE_TOOL_NAME}
    assert recorder["thinking"] == {"type": "disabled"}
    assert recorder["temperature"] == 0
    # 候选的 id 必须出现在提示里，否则模型没法引用它们
    assert "a" in str(recorder["messages"])
```

- [ ] **Step 6: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_edges.py -q
```
Expected: FAIL，`ImportError: cannot import name 'propose_all'`

- [ ] **Step 7: 实现连边编排与判定器**

追加到 `src/cn_curriculum_graph/pipeline/edges.py`：

```python
_SYSTEM = (
    "你是小学课标知识依赖图的构建者。"
    "会给你一个『目标知识点』和一组『候选前置知识点』，"
    "请挑出其中真正是目标知识点先修条件的那些。\n"
    "先修的判据：不先掌握候选，就学不动目标（hard）；"
    "或有助于理解但非必需（soft）。\n"
    "只能从给定候选里挑，必须原样引用它们的 id。挑不出就返回空列表 —— "
    "少一条边远好过一条错边，错边会静默地把学习路径导偏。\n"
    "reason 写一句中文，说明为什么它是前置，这句话会被直接拿去给学习者讲解。"
)

_TOOL = {
    "name": EDGE_TOOL_NAME,
    "description": "记录目标知识点的先修依赖",
    "input_schema": ProposedEdgeBatch.model_json_schema(),
}


class EdgeProposer(Protocol):
    def __call__(
        self, target: TopicDraft, candidates: list[TopicDraft]
    ) -> ProposedEdgeBatch: ...


class DeepSeekEdgeProposer:
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model

    def __call__(self, target: TopicDraft, candidates: list[TopicDraft]) -> ProposedEdgeBatch:
        listed = "\n".join(
            f"- id={c.draft_id}｜{c.content.name}（{c.content.grade_start}年级）："
            f"{c.content.description}"
            for c in candidates
        )
        prompt = (
            f"目标知识点\n"
            f"名称：{target.content.name}（{target.content.grade_start}年级）\n"
            f"描述：{target.content.description}\n\n"
            f"候选前置知识点\n{listed}"
        )
        response = self._client.messages.create(
            model=self._model,
            max_tokens=2048,
            temperature=0,
            system=_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": EDGE_TOOL_NAME},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == EDGE_TOOL_NAME:
                return ProposedEdgeBatch.model_validate(block.input)
        raise ValueError(f"模型未调用 {EDGE_TOOL_NAME} 工具，返回：{response.content!r}")


def propose_all(
    drafts: list[TopicDraft], proposer: EdgeProposer
) -> tuple[dict[str, list[ProposedEdge]], list[DropRecord]]:
    known = {d.draft_id for d in drafts}
    candidates = candidate_prerequisites(drafts)
    edges: dict[str, list[ProposedEdge]] = {d.draft_id: [] for d in drafts}
    drops: list[DropRecord] = []

    for target in drafts:
        pool = candidates[target.draft_id]
        if not pool:
            continue

        try:
            batch = proposer(target, pool)
        except Exception as exc:  # noqa: BLE001 —— 单个目标失败不影响其余
            drops.append(
                DropRecord(
                    stage="edges",
                    ref=target.draft_id,
                    reason="EDGES_FAILED",
                    detail=f"{type(exc).__name__}: {exc}",
                )
            )
            continue

        for edge in batch.edges:
            if edge.prerequisite_draft_id not in known:
                drops.append(
                    DropRecord(
                        stage="edges",
                        ref=target.draft_id,
                        reason="UNKNOWN_PREREQUISITE",
                        detail=f"模型引用了不存在的 id：{edge.prerequisite_draft_id}",
                    )
                )
                continue
            edges[target.draft_id].append(edge)

    return edges, drops
```

- [ ] **Step 8: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_edges.py -q
```
Expected: PASS，10 passed

- [ ] **Step 9: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/edges.py tests/pipeline/test_edges.py
git commit -m "feat(pipeline): edges 层，剪枝规则由校验规则反推

候选限定在年级不晚于目标且跨度 ≤2，调用次数 N² 降到 N。
模型引用不存在的 id 一律丢弃记账，不让它污染图。"
```

---

### Task 6: review 层（多 judge 投票）

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/review.py`
- Test: `tests/pipeline/test_review.py`

**Interfaces:**
- Consumes: `models.{TopicDraft, ProposedEdge, Vote, ReviewOutcome, DropRecord}`；`validators.consistency.{Judge, Verdict}`
- Produces: `FidelityJudge`（Protocol）、`DeepSeekFidelityJudge`、`FIDELITY_TOOL_NAME`、`review_drafts(drafts, fidelity_judges, name_judges) -> ReviewResult`、`review_edges(...)`

**审核维度与判据**：

| aspect | 判据 | 不通过的处理 |
|---|---|---|
| `fidelity` | `description` 是否出自 `source_span` | 丢弃 |
| `name_desc` | 复用三档 judge：`topic_mismatch` 不通过；`scope_mismatch` 通过但记录 | 丢弃 |
| `edge_reason` | `reason` 站不站得住 | 丢该边 |

**投票**：全票通过才算通过（分歧即淘汰）。

- [ ] **Step 1: 写失败测试**

创建 `tests/pipeline/test_review.py`：

```python
"""审核层：分歧即淘汰。

本轮定位是不承诺内容专业正确，那就宁可少产出也别放可疑的进去。
被淘汰的那批写进 dropped.json —— 它是最值得人工复核的清单，比随机抽检有价值。

⚠️ 当前双票是同族（deepseek-v4-flash + v4-pro），独立性打折。
理想是跨训练谱系互投，配上 ANTHROPIC_API_KEY 即可切换。
"""

from types import SimpleNamespace

from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, TopicDraft
from cn_curriculum_graph.pipeline.review import (
    FIDELITY_TOOL_NAME,
    DeepSeekFidelityJudge,
    review_drafts,
    review_edges,
)
from cn_curriculum_graph.validators.consistency import Verdict


def _draft(draft_id: str = "a", name: str = "小数的意义") -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id="c#001",
        standard_codes=["3.1.2"],
        content=DraftContent(
            name=name,
            description="理解小数表示十进制分数",
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=4,
            grade_end=4,
            evidence=["证据"],
            assessment_prompt="问一句",
            source_span="能理解小数的意义",
        ),
    )


def _fidelity(approved: bool, reviewer: str = "fake"):
    from cn_curriculum_graph.pipeline.models import Vote

    def judge(draft: TopicDraft) -> Vote:
        return Vote(reviewer=reviewer, approved=approved, reason="测试")

    return judge


def _name_judge(judgment: str):
    def judge(name: str, description: str) -> Verdict:
        return Verdict(judgment=judgment, reason="测试")

    return judge


def test_a_draft_approved_by_everyone_is_kept():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲"), _fidelity(True, "乙")],
        name_judges=[_name_judge("consistent")],
    )

    assert [d.draft_id for d in result.kept] == ["a"]
    assert result.drops == []


def test_split_fidelity_vote_drops_the_draft():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲"), _fidelity(False, "乙")],
        name_judges=[_name_judge("consistent")],
    )

    assert result.kept == []
    assert result.drops[0].reason == "REVIEW_REJECTED"
    assert "fidelity" in result.drops[0].detail


def test_topic_mismatch_drops_the_draft():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True)],
        name_judges=[_name_judge("topic_mismatch")],
    )

    assert result.kept == []
    assert "name_desc" in result.drops[0].detail


def test_scope_mismatch_keeps_the_draft_but_records_the_outcome():
    """范围不符是 WARNING 级 —— 保留，但要留痕，跟校验层的两级严重性一致。"""
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True)],
        name_judges=[_name_judge("scope_mismatch")],
    )

    assert [d.draft_id for d in result.kept] == ["a"]
    scoped = [o for o in result.outcomes if o.aspect == "name_desc"]
    assert scoped[0].approved is True
    assert "scope_mismatch" in scoped[0].votes[0].reason


def test_every_decision_is_recorded_even_when_approved():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲")],
        name_judges=[_name_judge("consistent")],
    )

    assert {o.aspect for o in result.outcomes} == {"fidelity", "name_desc"}


def test_edges_are_reviewed_and_rejected_ones_dropped():
    from cn_curriculum_graph.pipeline.models import Vote

    def approve(target, edge):
        return Vote(reviewer="甲", approved=edge.prerequisite_draft_id == "good", reason="测试")

    edges = {
        "a": [
            ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住"),
            ProposedEdge(prerequisite_draft_id="bad", strength="soft", reason="站不住"),
        ]
    }

    result = review_edges({"a": _draft("a")}, edges, edge_judges=[approve])

    assert [e.prerequisite_draft_id for e in result.kept_edges["a"]] == ["good"]
    assert result.drops[0].reason == "REVIEW_REJECTED"
    assert "bad" in result.drops[0].detail


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", name=FIDELITY_TOOL_NAME, input=tool_input)
            ]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_fidelity_judge_shows_both_description_and_source_span():
    recorder: dict = {}
    client = _fake_client(recorder, {"approved": False, "reason": "描述超出原文"})

    vote = DeepSeekFidelityJudge(client=client, model="deepseek-v4-pro")(_draft())

    assert vote.approved is False
    assert vote.reviewer == "deepseek-v4-pro"
    payload = str(recorder["messages"])
    assert "理解小数表示十进制分数" in payload
    assert "能理解小数的意义" in payload
    assert recorder["tool_choice"] == {"type": "tool", "name": FIDELITY_TOOL_NAME}
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_review.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.review'`

- [ ] **Step 3: 实现审核层**

创建 `src/cn_curriculum_graph/pipeline/review.py`：

```python
"""交叉审核：多个判定器各投一票，全票通过才留。

**分歧即淘汰**，因为本轮不承诺内容专业正确 —— 宁可少产出也别放可疑的进去。
被淘汰的写进 dropped.json，那是最值得人工复核的清单。

⚠️ **已知短板**：默认双票是 deepseek-v4-flash + v4-pro，同族模型的误判
高度相关，投两次约等于投一次。理想是 Anthropic + DeepSeek 跨训练谱系互投；
配上 ANTHROPIC_API_KEY 后把 judges 列表换掉即可。这一点必须写进产出物说明，
不能让人误读成"两个模型都同意所以可信"。
"""

from __future__ import annotations

import os
from typing import Any, Protocol

from pydantic import BaseModel, ConfigDict

from cn_curriculum_graph.judges.deepseek_judge import DEEPSEEK_BASE_URL
from cn_curriculum_graph.pipeline.models import (
    DropRecord,
    ProposedEdge,
    ReviewOutcome,
    TopicDraft,
    Vote,
)
from cn_curriculum_graph.validators.consistency import Judge

DEFAULT_MODEL = "deepseek-v4-flash"
FIDELITY_TOOL_NAME = "record_fidelity"
EDGE_REVIEW_TOOL_NAME = "record_edge_review"


class _VotePayload(BaseModel):
    """判定器工具的 input_schema。reviewer 由代码填，不问模型。"""

    model_config = ConfigDict(extra="forbid")

    approved: bool
    reason: str = ""


class FidelityJudge(Protocol):
    def __call__(self, draft: TopicDraft) -> Vote: ...


class EdgeJudge(Protocol):
    def __call__(self, target: TopicDraft, edge: ProposedEdge) -> Vote: ...


class ReviewResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kept: list[TopicDraft] = []
    kept_edges: dict[str, list[ProposedEdge]] = {}
    outcomes: list[ReviewOutcome] = []
    drops: list[DropRecord] = []


_FIDELITY_SYSTEM = (
    "你是知识图谱的数据质检员。"
    "会给你一个知识点的『描述』和它声称的『原文出处』，"
    "判断描述是不是确实出自这句原文。\n"
    "判 approved=false：描述引入了原文里没有的内容，或与原文说的不是一回事。\n"
    "判 approved=true：描述是对原文的忠实转写或合理概括。\n"
    "措辞不同、更具体、更书面都算忠实；凭空增加的知识点不算。\n"
    "reason 用一句中文说明依据。"
)

_EDGE_SYSTEM = (
    "你是小学课标知识依赖图的数据质检员。"
    "会给你一条先修依赖边：目标知识点、前置知识点，以及声称的理由。\n"
    "判 approved=true：这个前置关系成立，且理由说得通。\n"
    "判 approved=false：前置关系不成立，或理由与实际关系对不上。\n"
    "拿不准判 false —— 一条错边会静默地把学习路径导偏，代价远大于少一条边。\n"
    "reason 用一句中文说明依据。"
)


class _DeepSeekVoter:
    def __init__(self, tool_name: str, system: str, client: Any | None, model: str) -> None:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(
                base_url=DEEPSEEK_BASE_URL, api_key=os.environ["DEEPSEEK_API_KEY"]
            )
        self._client = client
        self._model = model
        self._tool_name = tool_name
        self._system = system
        self._tool = {
            "name": tool_name,
            "description": "记录审核结论",
            "input_schema": _VotePayload.model_json_schema(),
        }

    def _vote(self, prompt: str) -> Vote:
        response = self._client.messages.create(
            model=self._model,
            max_tokens=1024,
            temperature=0,
            system=self._system,
            messages=[{"role": "user", "content": prompt}],
            tools=[self._tool],
            tool_choice={"type": "tool", "name": self._tool_name},
            thinking={"type": "disabled"},
        )
        for block in response.content:
            if block.type == "tool_use" and block.name == self._tool_name:
                payload = _VotePayload.model_validate(block.input)
                # reviewer 由代码填 —— 模型不该自报家门
                return Vote(
                    reviewer=self._model, approved=payload.approved, reason=payload.reason
                )
        raise ValueError(f"模型未调用 {self._tool_name} 工具，返回：{response.content!r}")


class DeepSeekFidelityJudge(_DeepSeekVoter):
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        super().__init__(FIDELITY_TOOL_NAME, _FIDELITY_SYSTEM, client, model)

    def __call__(self, draft: TopicDraft) -> Vote:
        return self._vote(
            f"原文出处：{draft.content.source_span}\n\n描述：{draft.content.description}"
        )


class DeepSeekEdgeJudge(_DeepSeekVoter):
    def __init__(self, client: Any | None = None, model: str = DEFAULT_MODEL) -> None:
        super().__init__(EDGE_REVIEW_TOOL_NAME, _EDGE_SYSTEM, client, model)

    def __call__(self, target: TopicDraft, edge: ProposedEdge) -> Vote:
        return self._vote(
            f"目标知识点：{target.content.name} —— {target.content.description}\n"
            f"前置知识点 id：{edge.prerequisite_draft_id}\n"
            f"强度：{edge.strength}\n"
            f"声称的理由：{edge.reason}"
        )


def review_drafts(
    drafts: list[TopicDraft],
    fidelity_judges: list[FidelityJudge],
    name_judges: list[Judge],
) -> ReviewResult:
    kept: list[TopicDraft] = []
    outcomes: list[ReviewOutcome] = []
    drops: list[DropRecord] = []

    for draft in drafts:
        rejected: list[str] = []

        fidelity_votes = [judge(draft) for judge in fidelity_judges]
        fidelity_ok = all(v.approved for v in fidelity_votes)
        outcomes.append(
            ReviewOutcome(
                target=draft.draft_id,
                aspect="fidelity",
                votes=fidelity_votes,
                approved=fidelity_ok,
            )
        )
        if not fidelity_ok:
            rejected.append("fidelity")

        # 名实一致复用三档 judge：topic_mismatch 才淘汰，
        # scope_mismatch 是 WARNING 级，保留但留痕 —— 与校验层的两级严重性一致
        name_votes: list[Vote] = []
        for judge in name_judges:
            verdict = judge(name=draft.content.name, description=draft.content.description)
            name_votes.append(
                Vote(
                    reviewer="name_desc",
                    approved=verdict.judgment != "topic_mismatch",
                    reason=f"{verdict.judgment}: {verdict.reason}",
                )
            )
        name_ok = all(v.approved for v in name_votes)
        outcomes.append(
            ReviewOutcome(
                target=draft.draft_id, aspect="name_desc", votes=name_votes, approved=name_ok
            )
        )
        if not name_ok:
            rejected.append("name_desc")

        if rejected:
            drops.append(
                DropRecord(
                    stage="review",
                    ref=draft.draft_id,
                    reason="REVIEW_REJECTED",
                    detail=f"未通过的维度：{','.join(rejected)}",
                )
            )
            continue
        kept.append(draft)

    return ReviewResult(kept=kept, outcomes=outcomes, drops=drops)


def review_edges(
    drafts_by_id: dict[str, TopicDraft],
    edges: dict[str, list[ProposedEdge]],
    edge_judges: list[EdgeJudge],
) -> ReviewResult:
    kept_edges: dict[str, list[ProposedEdge]] = {}
    outcomes: list[ReviewOutcome] = []
    drops: list[DropRecord] = []

    for target_id, proposed in edges.items():
        target = drafts_by_id[target_id]
        kept_edges[target_id] = []
        for edge in proposed:
            votes = [judge(target, edge) for judge in edge_judges]
            approved = all(v.approved for v in votes)
            outcomes.append(
                ReviewOutcome(
                    target=f"{target_id}<-{edge.prerequisite_draft_id}",
                    aspect="edge_reason",
                    votes=votes,
                    approved=approved,
                )
            )
            if approved:
                kept_edges[target_id].append(edge)
            else:
                drops.append(
                    DropRecord(
                        stage="review",
                        ref=target_id,
                        reason="REVIEW_REJECTED",
                        detail=f"边 {target_id}<-{edge.prerequisite_draft_id} 未通过审核",
                    )
                )

    return ReviewResult(kept_edges=kept_edges, outcomes=outcomes, drops=drops)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_review.py -q
```
Expected: PASS，8 passed

- [ ] **Step 5: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/review.py tests/pipeline/test_review.py
git commit -m "feat(pipeline): review 层，三维度多判定器投票，分歧即淘汰

name_desc 复用三档 judge：topic_mismatch 淘汰、scope_mismatch 保留留痕，
与校验层两级严重性一致。双票当前同族，独立性折扣已写进模块文档。"
```

---

### Task 7: assemble 层（id、provenance、组装）

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/assemble.py`
- Test: `tests/pipeline/test_assemble.py`

**Interfaces:**
- Consumes: `models.{TopicDraft, ProposedEdge}`；`cn_curriculum_graph.models.{CurriculumGraph, Topic, Dependency, Provenance, Standard}`
- Produces: `make_topic_id(content) -> str`、`assemble(drafts, edges, model_id, curriculum) -> CurriculumGraph`

- [ ] **Step 1: 写失败测试**

创建 `tests/pipeline/test_assemble.py`：

```python
"""组装层：把流水线内部类型翻译成对外 schema，补齐代码负责的字段。

最要紧的一条：provenance 由代码填死，confidence 恒为 0.0。
自己声明自己可信是没有意义的 —— 这正是 Marble 最大的信任缺口，
本项目的立身之本就是修掉它。
"""

import pytest

from cn_curriculum_graph.pipeline.assemble import assemble, make_topic_id
from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, TopicDraft


def _draft(draft_id: str, name: str, grade: int = 4) -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id="c#001",
        standard_codes=["3.1.2"],
        content=DraftContent(
            name=name,
            description="描述",
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=grade,
            grade_end=grade,
            evidence=["证据"],
            assessment_prompt="问一句",
            source_span="原文",
        ),
    )


def test_topic_id_is_deterministic_and_prefixed():
    left, right = _draft("a", "小数的意义"), _draft("b", "小数的意义")

    assert make_topic_id(left.content) == make_topic_id(right.content)
    assert make_topic_id(left.content).startswith("t_")


def test_topic_id_differs_when_grade_differs():
    early, late = _draft("a", "认识角", grade=4), _draft("b", "认识角", grade=7)

    assert make_topic_id(early.content) != make_topic_id(late.content)


def test_provenance_is_filled_by_code_with_zero_confidence():
    graph = assemble([_draft("a", "小数的意义")], {}, model_id="deepseek-v4-flash",
                     curriculum="cn-moe-math-2022")

    prov = graph.topics[0].provenance
    assert prov.method == "llm-extract/deepseek-v4-flash"
    assert prov.review_status == "unreviewed"
    # 没人审过，任何非零置信度都是自欺
    assert prov.confidence == 0.0
    assert prov.reviewer is None


def test_standard_codes_become_standards_with_the_curriculum_constant():
    graph = assemble([_draft("a", "小数的意义")], {}, model_id="m", curriculum="cn-moe-math-2022")

    (standard,) = graph.topics[0].standards
    assert standard.curriculum == "cn-moe-math-2022"
    assert standard.code == "3.1.2"


def test_source_span_does_not_leak_into_the_output_schema():
    graph = assemble([_draft("a", "小数的意义")], {}, model_id="m", curriculum="c")

    assert "source_span" not in graph.topics[0].model_dump()


def test_edges_are_translated_using_topic_ids_not_draft_ids():
    early, late = _draft("a", "凑十", grade=3), _draft("b", "两位数加法", grade=4)
    edges = {"b": [ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="先会凑十")]}

    graph = assemble([early, late], edges, model_id="m", curriculum="c")

    (dep,) = graph.dependencies
    assert dep.topic_id == make_topic_id(late.content)
    assert dep.prerequisite_id == make_topic_id(early.content)
    assert dep.reason == "先会凑十"


def test_id_collision_raises_instead_of_silently_overwriting():
    """三项全同说明本该在 dedupe 阶段合并 —— 静默覆盖会丢数据。"""
    twin_a, twin_b = _draft("a", "小数的意义"), _draft("b", "小数的意义")

    with pytest.raises(ValueError, match="id 碰撞"):
        assemble([twin_a, twin_b], {}, model_id="m", curriculum="c")
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_assemble.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.assemble'`

- [ ] **Step 3: 实现组装层**

创建 `src/cn_curriculum_graph/pipeline/assemble.py`：

```python
"""把流水线内部类型翻译成对外 schema。

这一层负责补齐**代码该填的字段**：id、provenance、standards[].curriculum。
理由见 docs/pipeline-design.md §5 —— 模型只产出它有资格产出的东西。
"""

from __future__ import annotations

import hashlib

from cn_curriculum_graph.models import (
    CurriculumGraph,
    Dependency,
    Provenance,
    Standard,
    Topic,
)
from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, TopicDraft


def make_topic_id(content: DraftContent) -> str:
    """确定性 id：重排输入不改变结果，便于比对两次跑的差异。

    取 (name, domain, grade_start) 而非序号，是为了让 id 在增删条目时保持稳定。
    """
    seed = f"{content.name}|{content.domain}|{content.grade_start}"
    return "t_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def assemble(
    drafts: list[TopicDraft],
    edges: dict[str, list[ProposedEdge]],
    model_id: str,
    curriculum: str,
) -> CurriculumGraph:
    provenance = Provenance(
        method=f"llm-extract/{model_id}",
        review_status="unreviewed",
        # 没有任何教研审核，非零置信度都是自欺。投票结果单独记进
        # review-log.json —— 模型间的一致程度和教研正确性是两件事。
        confidence=0.0,
    )

    topic_id_by_draft: dict[str, str] = {}
    topics: list[Topic] = []
    for draft in drafts:
        topic_id = make_topic_id(draft.content)
        if topic_id in topic_id_by_draft.values():
            raise ValueError(
                f"id 碰撞：{topic_id}（{draft.content.name}）—— "
                "名称、领域、起始年级三项全同，本该在 dedupe 阶段合并"
            )
        topic_id_by_draft[draft.draft_id] = topic_id
        topics.append(
            Topic(
                id=topic_id,
                name=draft.content.name,
                description=draft.content.description,
                type=draft.content.type,
                subject=draft.content.subject,
                domain=draft.content.domain,
                grade_start=draft.content.grade_start,
                grade_end=draft.content.grade_end,
                evidence=draft.content.evidence,
                assessment_prompt=draft.content.assessment_prompt,
                misconceptions=draft.content.misconceptions,
                standards=[Standard(curriculum=curriculum, code=c) for c in draft.standard_codes],
                provenance=provenance,
            )
        )

    dependencies = [
        Dependency(
            topic_id=topic_id_by_draft[target_draft_id],
            prerequisite_id=topic_id_by_draft[edge.prerequisite_draft_id],
            strength=edge.strength,
            reason=edge.reason,
        )
        for target_draft_id, proposed in edges.items()
        for edge in proposed
        if target_draft_id in topic_id_by_draft
        and edge.prerequisite_draft_id in topic_id_by_draft
    ]

    return CurriculumGraph(topics=topics, dependencies=dependencies)
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_assemble.py -q
```
Expected: PASS，7 passed

- [ ] **Step 5: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/assemble.py tests/pipeline/test_assemble.py
git commit -m "feat(pipeline): assemble 层，provenance 由代码填死 confidence=0.0

没有教研审核时任何非零置信度都是自欺 —— 这正是 Marble 的信任缺口。
id 碰撞直接报错而非静默覆盖。"
```

---

### Task 8: 编排、CLI 与端到端

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/run.py`
- Modify: `pyproject.toml`（新增 `ccg-generate` 入口）
- Create: `data/source/example.md`
- Test: `tests/pipeline/test_run.py`

**Interfaces:**
- Consumes: 前七个 Task 的全部产出
- Produces: `STAGES`、`PipelineDeps`、`run_pipeline(...)`、`main(argv) -> int`

- [ ] **Step 1: 写端到端失败测试**

创建 `tests/pipeline/test_run.py`：

```python
"""端到端：全 fake 跑一遍完整管道，断言产出能过 run_all 且 0 error。

不为真模型的输出写断言 —— 内容正确性不在本轮承诺范围内，
为它写断言等于假装能验证。真模型的验证靠手动跑一次 + 人眼看中间产物。
"""

import json

from cn_curriculum_graph.pipeline.models import (
    DraftBatch,
    DraftContent,
    ProposedEdge,
    ProposedEdgeBatch,
    Vote,
)
from cn_curriculum_graph.pipeline.run import PipelineDeps, run_pipeline
from cn_curriculum_graph.runner import has_errors
from cn_curriculum_graph.validators.consistency import Verdict

SOURCE = """3.1.1 能认识并读写 100 以内的数。

3.1.2 能计算 100 以内的加减法。
"""


def _content(name: str, grade: int, span: str) -> DraftContent:
    return DraftContent(
        name=name,
        description=f"{name}的具体内容",
        type="conceptual",
        subject="数学",
        domain="数与代数",
        grade_start=grade,
        grade_end=grade,
        evidence=[f"能演示{name}"],
        assessment_prompt=f"说说{name}？",
        source_span=span,
    )


def _fake_deps() -> PipelineDeps:
    def extractor(chunk):
        if chunk.standard_code == "3.1.1":
            return DraftBatch(drafts=[_content("认识100以内的数", 1, chunk.text)])
        return DraftBatch(drafts=[_content("100以内加减法", 2, chunk.text)])

    def same_topic(a, b):
        from cn_curriculum_graph.pipeline.dedupe import SameTopicVerdict

        return SameTopicVerdict(same=False, reason="不同")

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[
                ProposedEdge(
                    prerequisite_draft_id=candidates[0].draft_id,
                    strength="hard",
                    reason="先会读写才能算",
                )
            ]
        )

    return PipelineDeps(
        extractor=extractor,
        same_topic_judge=same_topic,
        edge_proposer=proposer,
        fidelity_judges=[lambda d: Vote(reviewer="fake", approved=True, reason="ok")],
        name_judges=[lambda name, description: Verdict(judgment="consistent")],
        edge_judges=[lambda t, e: Vote(reviewer="fake", approved=True, reason="ok")],
    )


def test_end_to_end_produces_a_graph_that_passes_validation(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    findings = run_pipeline(
        source_dir=source.parent,
        out_dir=out,
        deps=_fake_deps(),
        model_id="fake",
        curriculum="cn-moe-math-2022",
    )

    assert not has_errors(findings)


def test_every_stage_lands_a_readable_file(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    run_pipeline(source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    for name in (
        "01-chunks.json",
        "02-drafts.json",
        "03-deduped.json",
        "04-edges.json",
        "05-reviewed.json",
        "graph.json",
    ):
        assert (out / name).exists(), f"缺少中间产物 {name}"


def test_generated_graph_records_zero_confidence(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    source.write_text(SOURCE, encoding="utf-8")
    out = tmp_path / "out"

    run_pipeline(source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    graph = json.loads((out / "graph.json").read_text(encoding="utf-8"))
    assert all(t["provenance"]["confidence"] == 0.0 for t in graph["topics"])
    assert all(t["provenance"]["review_status"] == "unreviewed" for t in graph["topics"])


def test_dropped_records_accumulate_across_stages(tmp_path):
    source = tmp_path / "source" / "math.md"
    source.parent.mkdir(parents=True)
    # 第三段没有条目编号 —— chunk 层应当丢弃并记账
    source.write_text(SOURCE + "\n这一段是导言，没有编号。\n", encoding="utf-8")
    out = tmp_path / "out"

    run_pipeline(source.parent, out, _fake_deps(), model_id="fake", curriculum="c")

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    assert any(d["reason"] == "NO_STANDARD_CODE" for d in drops)
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_run.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.run'`

- [ ] **Step 3: 实现编排**

创建 `src/cn_curriculum_graph/pipeline/run.py`：

```python
"""编排六层，逐层落盘。

每层跑完就写文件再进下一层 —— 这不是为了性能，是为了**可人眼检查**。
看不见中间状态就没法判断它到底在干什么（effective-agents 心法③）。
"""

from __future__ import annotations

import argparse
import os
from dataclasses import dataclass, field
from pathlib import Path

from cn_curriculum_graph.judges.deepseek_judge import DeepSeekJudge
from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import chunk as chunk_mod
from cn_curriculum_graph.pipeline import dedupe as dedupe_mod
from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.models import Chunk, ProposedEdge, TopicDraft
from cn_curriculum_graph.runner import has_errors, run_all
from cn_curriculum_graph.validators.base import Finding

DEFAULT_CURRICULUM = "cn-moe-math-2022"
STAGES = ("chunk", "extract", "dedupe", "edges", "review", "assemble")


@dataclass
class PipelineDeps:
    """全部外部依赖集中在这里注入 —— 测试传 fake，生产传真 LLM。"""

    extractor: extract_mod.Extractor
    same_topic_judge: dedupe_mod.SameTopicJudge
    edge_proposer: edges_mod.EdgeProposer
    fidelity_judges: list[review_mod.FidelityJudge] = field(default_factory=list)
    name_judges: list = field(default_factory=list)
    edge_judges: list[review_mod.EdgeJudge] = field(default_factory=list)


def build_deepseek_deps(models: list[str]) -> PipelineDeps:
    """默认投票者是同族双票（flash + pro），独立性打折 —— 见 review.py 模块文档。"""
    primary = models[0]
    return PipelineDeps(
        extractor=extract_mod.DeepSeekExtractor(model=primary),
        same_topic_judge=dedupe_mod.DeepSeekSameTopicJudge(model=primary),
        edge_proposer=edges_mod.DeepSeekEdgeProposer(model=primary),
        fidelity_judges=[review_mod.DeepSeekFidelityJudge(model=m) for m in models],
        name_judges=[DeepSeekJudge(model=m) for m in models],
        edge_judges=[review_mod.DeepSeekEdgeJudge(model=m) for m in models],
    )


def run_pipeline(
    source_dir: Path,
    out_dir: Path,
    deps: PipelineDeps,
    model_id: str,
    curriculum: str = DEFAULT_CURRICULUM,
) -> list[Finding]:
    out_dir.mkdir(parents=True, exist_ok=True)
    drops_path = out_dir / "dropped.json"

    # 1 chunk
    chunks: list[Chunk] = []
    for path in sorted(source_dir.glob("*.md")):
        produced, dropped = chunk_mod.split_source(
            path.read_text(encoding="utf-8"), source_file=path.name
        )
        chunks += produced
        io.append_drops(drops_path, dropped)
    io.write_stage(out_dir / "01-chunks.json", chunks)

    # 2 extract
    drafts, dropped = extract_mod.extract_all(chunks, deps.extractor)
    io.append_drops(drops_path, dropped)
    io.write_stage(out_dir / "02-drafts.json", drafts)

    # 3 dedupe
    deduped = dedupe_mod.dedupe(drafts, deps.same_topic_judge)
    io.append_drops(drops_path, deduped.drops)
    io.write_stage(out_dir / "03-deduped.json", deduped.kept)
    io.write_stage(out_dir / "merges.json", deduped.merges)

    # 4 edges
    proposed, dropped = edges_mod.propose_all(deduped.kept, deps.edge_proposer)
    io.append_drops(drops_path, dropped)
    io.write_stage(
        out_dir / "04-edges.json",
        [e for group in proposed.values() for e in group],
    )

    # 5 review
    draft_review = review_mod.review_drafts(
        deduped.kept, deps.fidelity_judges, deps.name_judges
    )
    io.append_drops(drops_path, draft_review.drops)
    kept_ids = {d.draft_id for d in draft_review.kept}
    surviving_edges = {
        target: [e for e in group if e.prerequisite_draft_id in kept_ids]
        for target, group in proposed.items()
        if target in kept_ids
    }
    edge_review = review_mod.review_edges(
        {d.draft_id: d for d in draft_review.kept}, surviving_edges, deps.edge_judges
    )
    io.append_drops(drops_path, edge_review.drops)
    io.write_stage(out_dir / "05-reviewed.json", draft_review.kept)
    io.write_stage(
        out_dir / "review-log.json", draft_review.outcomes + edge_review.outcomes
    )

    # 6 assemble + 校验
    graph = assemble_mod.assemble(
        draft_review.kept, edge_review.kept_edges, model_id=model_id, curriculum=curriculum
    )
    (out_dir / "graph.json").write_text(
        graph.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
    )
    return run_all(graph)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="ccg-generate", description="从课标原文生成知识依赖图"
    )
    parser.add_argument("--source", type=Path, default=Path("data/source"))
    parser.add_argument("--out", type=Path, default=Path("data/generated"))
    parser.add_argument("--curriculum", default=DEFAULT_CURRICULUM)
    parser.add_argument(
        "--models",
        default="deepseek-v4-flash,deepseek-v4-pro",
        help="审核投票者，逗号分隔；第一个同时用于抽取与连边",
    )
    args = parser.parse_args(argv)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("需要 DEEPSEEK_API_KEY（export 或写进 .env）")

    models = [m.strip() for m in args.models.split(",") if m.strip()]
    findings = run_pipeline(
        source_dir=args.source,
        out_dir=args.out,
        deps=build_deepseek_deps(models),
        model_id=models[0],
        curriculum=args.curriculum,
    )

    from cn_curriculum_graph.cli import format_report

    print(format_report(findings))
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_run.py -q
```
Expected: PASS，4 passed

- [ ] **Step 5: 注册 CLI 入口**

修改 `pyproject.toml` 的 `[project.scripts]` 段，改成：

```toml
[project.scripts]
ccg-validate = "cn_curriculum_graph.cli:main"
ccg-generate = "cn_curriculum_graph.pipeline.run:main"
```

然后重新同步：

```bash
uv sync
uv run ccg-generate --help
```
Expected: 打印用法，含 `--source`、`--out`、`--models`

- [ ] **Step 6: 放一份示例素材**

创建 `data/source/example.md`：

```markdown
3.1.1 能认识、读、写万以内的数，会用数描述事物的多少与顺序。

3.1.2 能理解小数的意义，会比较小数的大小，会进行简单的小数加减运算。

3.1.3 能理解分数的意义，会比较同分母分数的大小。
```

- [ ] **Step 7: 跑全量测试**

```bash
uv run pytest -q
```
Expected: PASS，全绿

- [ ] **Step 8: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/run.py tests/pipeline/test_run.py pyproject.toml data/source
git commit -m "feat(pipeline): 六层编排、ccg-generate CLI 与端到端测试

全 fake 跑通完整管道并断言产出过 run_all 0 error。
每层落盘，dropped.json 跨层累加，没有静默跳过。"
```

- [ ] **Step 9: 接真模型跑一次（需 DEEPSEEK_API_KEY）**

```bash
uv run ccg-generate --source data/source --out data/generated
```

然后**人眼检查**这三样，这是本轮唯一的内容质量关：

1. `data/generated/02-drafts.json` —— `source_span` 是不是真出自原文
2. `data/generated/04-edges.json` —— `reason` 站不站得住
3. `data/generated/dropped.json` —— 丢掉的那些该不该丢

- [ ] **Step 10: 更新 README 与记忆，提交**

在 `README.md` 的「怎么跑」加一段生成流水线用法，「下一步」第 4 项标记完成；
按 `memory/README.md` 规则更新 `memory/learning-log.md`。

```bash
git add README.md ../../memory
git commit -m "docs(pipeline): README 补生成流水线用法，更新学习记忆"
git push origin main
```

---

## 自检记录

- **spec 覆盖**：设计文档 §2 输入契约 → Task 2 + Task 8 Step 6；§3.1-3.6 六层 → Task 2-7；§4 数据模型 → Task 1；§5 字段归属 → Task 1（结构约束）+ Task 7（provenance）；§6 错误处理 → 各层 drop 测试 + Task 1 落盘；§7 测试策略 → 全程 TDD + Task 8 端到端。无遗漏。
- **占位符**：无 TBD/TODO；每个代码步骤都给了完整可运行代码。
- **类型一致性**：`draft_id` / `chunk_id` / `standard_codes` / `content` 在 Task 1、3、4、5、6、7 中命名一致；`ProposedEdge.prerequisite_draft_id` 全程同名；`Vote.reviewer` 由代码填这一点在 Task 6 实现与测试中一致。
- **自检修正**：初稿在 `build_deepseek_deps` 里用 `__import__` 动态引入 `DeepSeekJudge` 以"规避循环引用"，但依赖方向是 `pipeline.* → judges.*` 单向，根本无环。已改回顶部普通 import —— 无谓的防御性写法会误导实现者去猜一个不存在的约束。
- **依赖方向**：`pipeline.*` 单向依赖 `judges.*` 与 `validators.*`，反向为零。新增模块时保持这个方向。
