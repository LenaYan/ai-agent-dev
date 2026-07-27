# 字面匹配 vs 向量检索受控对比 · 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把诊断检索的打分函数抽成可替换的一层，接上本地 embedding 模型，在同一份 22 条 ground truth 上量出「向量 vs 字面」的受控对比数字与真实代价。

**Architecture:** 新增 `Scorer` 协议（`serve/scoring.py`）承载"两段文本有多相关"，字面版把现有逻辑原样搬入，向量版靠注入的 `Embedder` 算余弦。**聚合逻辑（主/次字段加权、过门槛即入选）一行不改** —— 这是受控对比的全部前提。阈值挂在打分器上（余弦与覆盖率标度不同），聚合权重留在 `query.py`（两条路线必须一致）。真模型只在 eval 脚本里加载，领域层测试注入假 embedder。

**Tech Stack:** Python 3.12 · uv · pydantic · pytest · sentence-transformers（可选依赖）· BAAI/bge-m3 · numpy

## Global Constraints

- **不改聚合权重**：`DESCRIPTION_WEIGHT = 0.5`、`SECONDARY_WEIGHT = 0.5` 及"任一字段过门槛即入选、加权分只用于排序"的结构，两条路线必须完全一致。改了就不是受控对比。
- **领域层不 import 模型库**：`serve/query.py` 与 `serve/scoring.py` 一行都不许出现 `torch` / `transformers` / `sentence_transformers`。只有 `serve/embedding.py` 可以。
- **CI 不装可选依赖**：`sentence-transformers` 进 `[project.optional-dependencies]`，`uv sync` 默认不拉；全部单测必须在**没装它**的环境里绿。
- **MCP server 默认走字面检索**：本轮不改 `serve/mcp_server.py` 的默认行为。
- **阈值不许调到刚好过闸门**：向量版的阈值由扫描前沿得出，并在「空样本正确率 = 100%」这个与字面基线相同的工作点上比较 recall@3。
- **TDD**：每个任务先写失败测试、看它失败、再写最小实现。
- 注释与提交信息用中文；术语保留英文原词。
- Python 3.12；一律 `uv run` 执行。

---

### Task 1: 抽出 `Scorer` 协议与 `LiteralScorer`（纯重构）

把现有的字面打分原样搬进新一层，行为不变。现有 55 条 `test_query.py` 测试是这次重构的守门人 —— 它们一行不改仍须全绿。

**Files:**
- Create: `src/cn_curriculum_graph/serve/scoring.py`
- Modify: `src/cn_curriculum_graph/serve/query.py`
- Modify: `tests/serve/test_query.py`（只改 `normalize_math` 的 import 来源）
- Test: `tests/serve/test_scoring.py`

**Interfaces:**
- Consumes: 现有 `serve/query.py` 中的 `normalize_math` / `_normalize` / `_grams` / `_relevance` / `MIN_RELEVANCE`
- Produces:
  - `serve.scoring.Scorer`（Protocol）：属性 `min_relevance: float`；方法 `warm(texts: Sequence[str]) -> None`、`relevance(query: str, target: str) -> float`
  - `serve.scoring.LiteralScorer`（class，无构造参数，`min_relevance = 0.2`）
  - `serve.scoring.normalize_math(text: str) -> str`（从 query.py 迁入）
  - `serve.query.GraphIndex.__init__(graph, *, scorer: Scorer | None = None)`，属性 `index.scorer`
  - `serve.query.GraphIndex.search_texts(topic_id) -> tuple[str, str]`（取代 `search_grams`，返回 `(name, description)` 原文）

- [ ] **Step 1: 写失败测试 —— Scorer 协议与 LiteralScorer 的契约**

创建 `tests/serve/test_scoring.py`：

```python
"""打分器这一层的契约测试。

这一层存在的理由只有一个：让"两段文本有多相关"可替换，而聚合逻辑不动。
所以这里测的是**协议契约**（阈值、warm、relevance 的取值范围），
不测检索效果 —— 那是 eval 的事。
"""

from cn_curriculum_graph.serve.scoring import LiteralScorer, normalize_math


def test_literal_scorer_carries_its_own_threshold():
    """阈值是打分器的性质，不是检索的性质：余弦与覆盖率的标度完全不同，
    同一个 0.2 在两者上不是一回事。"""
    assert LiteralScorer().min_relevance == 0.2


def test_literal_relevance_is_symmetric_enough_to_beat_length_penalty():
    """长观察句命中短 statement —— 这是上一轮把 recall@3 从 63% 修到 84%
    的两处修复之一，搬到新一层后必须还在。"""
    scorer = LiteralScorer()
    long_query = "他说减法也能交换，5 减 3 和 3 减 5 反正结果一样，写哪个都行。"

    assert scorer.relevance(long_query, "减法也有交换律，比如 5-3=3-5") >= 0.2


def test_literal_relevance_bridges_spoken_and_notation():
    """口语与符号的归一化 —— 另一处修复，同样必须还在。"""
    scorer = LiteralScorer()

    assert scorer.relevance("孩子坚持八分之一比五分之一大", "1/8 比 1/5 大") >= 0.2


def test_literal_relevance_is_zero_for_unrelated_text():
    assert LiteralScorer().relevance("孩子背古诗老记不住", "1/8 比 1/5 大") == 0.0


def test_literal_warm_is_a_no_op():
    """字面打分不需要预热，但协议要求这个方法存在 —— 向量版靠它做批量编码。"""
    scorer = LiteralScorer()

    scorer.warm(["随便", "几条", "文本"])

    assert scorer.relevance("随便", "随便") > 0


def test_normalize_math_moved_but_unchanged():
    assert normalize_math("减法也有交换律") == "减法也有交换律"
    assert normalize_math("5 减 3") == "5-3"
    assert normalize_math("八分之一") == "1/8"
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/serve/test_scoring.py -q`
Expected: FAIL —— `ModuleNotFoundError: No module named 'cn_curriculum_graph.serve.scoring'`

- [ ] **Step 3: 创建 `serve/scoring.py`，把字面打分搬进来**

从 `serve/query.py` **剪切**（不是复制）这些符号到新文件：`_CN_DIGITS`、`_OPERATORS`、`_NUMBER`、`_as_digits`、`normalize_math`、`_normalize`、`_grams`、`_relevance`、`MIN_RELEVANCE`（改名为 `LiteralScorer.min_relevance`），连同它们的整段中文注释一起搬 —— 那些注释记着两次修复的原因，丢了就等于丢了记录。

```python
"""打分器：把"两段文本有多相关"抽成可替换的一层。

**为什么阈值也挂在打分器上**：余弦相似度与字面覆盖率的标度完全不同 ——
0.2 在覆盖率上是"敢返回空"的门槛，在余弦上什么都不是。阈值是打分器的
性质，不是检索的性质。

**为什么聚合权重不在这里**（`DESCRIPTION_WEIGHT` / `SECONDARY_WEIGHT` 留在
`query.py`）：那两个数在两条路线上必须完全一致，否则跑出来的差值说不清是
打分函数的功劳还是聚合的功劳。**只动一个变量**是这次对比的全部前提。
"""

from __future__ import annotations

import re
from typing import Protocol, Sequence

# ...（此处放入从 query.py 剪切过来的 _CN_DIGITS / _OPERATORS / _NUMBER /
#      _as_digits / normalize_math / _normalize / _grams / _relevance，
#      连同原有的全部中文注释，一字不改）


class Scorer(Protocol):
    """两段文本有多相关。

    `warm` 给实现一个批量预处理的机会（向量版在这里一次编码整个语料，
    避免退化成一条一条 encode）。字面版是空实现。
    """

    min_relevance: float

    def warm(self, texts: Sequence[str]) -> None: ...

    def relevance(self, query: str, target: str) -> float: ...


class LiteralScorer:
    """字面匹配：双向 gram 覆盖率取大者 + 数学表达归一化。

    这是 2026-07-27 那轮的产物，recall@3 = 84%（空样本正确率 100%）。
    它是这次对比的基线，行为不许在重构里发生任何改变。
    """

    min_relevance = 0.2

    def warm(self, texts: Sequence[str]) -> None:
        """字面打分无需预热。协议要求方法存在，向量版才用得上。"""

    def relevance(self, query: str, target: str) -> float:
        return _relevance(_grams(query), _grams(target))
```

- [ ] **Step 4: 运行新测试确认通过**

Run: `uv run pytest tests/serve/test_scoring.py -q`
Expected: PASS（6 passed）

- [ ] **Step 5: 改 `query.py` 走 scorer**

在 `serve/query.py` 中：

1. 删掉已搬走的符号，改为 `from cn_curriculum_graph.serve.scoring import LiteralScorer, Scorer`
2. `GraphIndex.__init__` 接受注入并预热：

```python
    def __init__(self, graph: CurriculumGraph, *, scorer: Scorer | None = None) -> None:
        self.graph = graph
        self.scorer: Scorer = scorer or LiteralScorer()
        # ...（原有的 by_id / _prereqs / _dependents / _revisits 构建不变）

        # 原来这里预算 grams；现在只存原文，预处理交给打分器
        self._search_text: dict[str, tuple[str, str]] = {
            t.id: (t.name, t.description) for t in graph.topics
        }
        self.scorer.warm(self._corpus_texts())

    def _corpus_texts(self) -> list[str]:
        """全部可被检索到的文本。向量打分器靠它一次编码完语料。"""
        texts: list[str] = []
        for t in self.graph.topics:
            texts.extend([t.name, t.description])
            for m in t.misconceptions:
                texts.extend([m.statement, m.probe, m.correction_hint])
        return texts

    def search_texts(self, topic_id: str) -> tuple[str, str]:
        """(name, description) 原文。打分器自己决定要不要预处理。"""
        return self._search_text[topic_id]
```

3. `search_topics` 与 `match_misconceptions` 里，把 `_relevance(grams, grams)` 换成 `index.scorer.relevance(原文, 原文)`，把 `MIN_RELEVANCE` 换成 `index.scorer.min_relevance`。**加权与入选结构一字不改**：

```python
def search_topics(index, query, *, grade=None, limit=5) -> list[TopicCard]:
    hits: list[tuple[float, str, Topic]] = []
    for topic in index.graph.topics:
        if grade is not None and not (topic.grade_start <= grade <= topic.grade_end):
            continue
        name, description = index.search_texts(topic.id)
        name_hit = index.scorer.relevance(query, name)
        desc_hit = index.scorer.relevance(query, description)
        if max(name_hit, desc_hit) < index.scorer.min_relevance:
            continue
        hits.append((max(name_hit, DESCRIPTION_WEIGHT * desc_hit), topic.id, topic))

    hits.sort(key=lambda h: (-h[0], h[1]))
    return [_card(topic, score) for score, _, topic in hits[:limit]]
```

`match_misconceptions` 同样处理：`primary = index.scorer.relevance(observation, mis.statement)`，`secondary = max(index.scorer.relevance(observation, mis.probe), index.scorer.relevance(observation, mis.correction_hint))`，门槛用 `index.scorer.min_relevance`，`score = max(primary, SECONDARY_WEIGHT * secondary)` 保持不变。

- [ ] **Step 6: 改 `tests/serve/test_query.py` 的 import**

把 `normalize_math` 从 `cn_curriculum_graph.serve.query` 改为从 `cn_curriculum_graph.serve.scoring` 导入。**其余一行不改** —— 这是重构是否等价的判据。

- [ ] **Step 7: 全套测试必须全绿**

Run: `uv run pytest -q`
Expected: PASS，总数 363 + 6 = **369 passed**。若 `test_query.py` 有任何一条红，说明重构不等价，回退重来 —— 不要改测试去迁就实现。

- [ ] **Step 8: 真实图上确认行为未变**

Run: `uv run python scripts/eval_diagnosis.py`
Expected: `recall@3 = 84%`、空样本正确率 `100%`、退出码 0。**数字必须与重构前逐字一致。**

- [ ] **Step 9: 提交**

```bash
git add -A
git commit -m "refactor(serve): 打分抽成 Scorer 协议，字面版原样搬入

为接向量检索做的准备，行为零变化：55 条 test_query.py 一行不改仍全绿，
真实图上 recall@3 仍是 84%、空样本 100%。

一处刻意的分工：阈值挂在打分器上（余弦与覆盖率标度不同，同一个 0.2
不是一回事），而聚合权重（主/次字段）留在 query.py —— 那两个数在两条
路线上必须完全一致，否则跑出来的差值说不清归谁。"
```

---

### Task 2: `Embedder` 协议 + `VectorScorer`（全程假 embedder）

**Files:**
- Create: `src/cn_curriculum_graph/serve/embedding.py`
- Modify: `src/cn_curriculum_graph/serve/scoring.py`
- Test: `tests/serve/test_vector_scoring.py`

**Interfaces:**
- Consumes: `serve.scoring.Scorer`（Task 1）
- Produces:
  - `serve.embedding.Embedder`（Protocol）：`encode(texts: list[str]) -> list[list[float]]`
  - `serve.scoring.VectorScorer(embedder: Embedder, *, min_relevance: float = 0.5)`，实现 `Scorer`
  - `serve.scoring.VectorScorer.encode_calls: int`（累计调用真 encode 的次数，供测试与代价度量读取）

- [ ] **Step 1: 写失败测试**

创建 `tests/serve/test_vector_scoring.py`：

```python
"""向量打分器的契约测试 —— 不加载任何模型。

这里测的是接线与病态输入，不是检索效果。效果由 scripts/eval_diagnosis.py
在真实 ground truth 上量，那才是这个实验的判据。
"""

import math

import pytest

from cn_curriculum_graph.serve.scoring import VectorScorer


class FakeEmbedder:
    """确定性假 embedder：按预置表返回向量，没登记的返回零向量。

    刻意不做"根据文本哈希生成向量"那种花招 —— 测试要能一眼看出
    每个断言为什么成立。
    """

    def __init__(self, table: dict[str, list[float]], *, default=(0.0, 0.0)):
        self.table = table
        self.default = list(default)
        self.calls: list[list[str]] = []

    def encode(self, texts: list[str]) -> list[list[float]]:
        self.calls.append(list(texts))
        return [self.table.get(t, list(self.default)) for t in texts]


def test_identical_vectors_score_one():
    scorer = VectorScorer(FakeEmbedder({"甲": [1.0, 0.0], "乙": [1.0, 0.0]}))

    assert scorer.relevance("甲", "乙") == pytest.approx(1.0)


def test_orthogonal_vectors_score_zero():
    scorer = VectorScorer(FakeEmbedder({"甲": [1.0, 0.0], "乙": [0.0, 1.0]}))

    assert scorer.relevance("甲", "乙") == pytest.approx(0.0)


def test_opposite_vectors_are_clamped_to_zero():
    """余弦可以是负的，相关度不行 —— 负相关度会让加权排序里的
    SECONDARY_WEIGHT 反向发力。"""
    scorer = VectorScorer(FakeEmbedder({"甲": [1.0, 0.0], "乙": [-1.0, 0.0]}))

    assert scorer.relevance("甲", "乙") == 0.0


def test_vectors_are_cached_so_each_text_is_encoded_once():
    """语料只有 150 条，但每次查询会跟每条比一遍 —— 不缓存就是
    每次查询几百次 encode，实验会慢到没法跑。"""
    fake = FakeEmbedder({"甲": [1.0, 0.0], "乙": [0.0, 1.0]})
    scorer = VectorScorer(fake)

    scorer.relevance("甲", "乙")
    scorer.relevance("甲", "乙")
    scorer.relevance("甲", "乙")

    encoded = [t for call in fake.calls for t in call]
    assert sorted(encoded) == ["乙", "甲"]


def test_warm_encodes_the_whole_corpus_in_one_call():
    fake = FakeEmbedder({})
    scorer = VectorScorer(fake)

    scorer.warm(["甲", "乙", "丙"])

    assert len(fake.calls) == 1
    assert sorted(fake.calls[0]) == ["丙", "乙", "甲"]


def test_warm_deduplicates_and_skips_cached_text():
    fake = FakeEmbedder({})
    scorer = VectorScorer(fake)

    scorer.warm(["甲", "甲", "乙"])
    scorer.warm(["乙", "丙"])

    assert sorted(fake.calls[0]) == ["乙", "甲"]
    assert fake.calls[1] == ["丙"]


def test_empty_text_scores_zero_without_encoding():
    """空 description 在生成的图里是可能的，不能让它变成一次 encode
    或一个 NaN。"""
    fake = FakeEmbedder({"甲": [1.0, 0.0]})
    scorer = VectorScorer(fake)

    assert scorer.relevance("甲", "") == 0.0
    assert scorer.relevance("   ", "甲") == 0.0
    assert fake.calls == []


def test_zero_vector_scores_zero_instead_of_dividing_by_zero():
    scorer = VectorScorer(FakeEmbedder({"甲": [0.0, 0.0], "乙": [1.0, 0.0]}))

    assert scorer.relevance("甲", "乙") == 0.0


def test_dimension_mismatch_raises_value_error():
    """维度不一致意味着 embedder 配置错了 —— 确定性错误，按
    docs/error-taxonomy.md 必须是 ValueError，不可重试。"""
    scorer = VectorScorer(FakeEmbedder({"甲": [1.0, 0.0], "乙": [1.0, 0.0, 0.0]}))

    with pytest.raises(ValueError):
        scorer.relevance("甲", "乙")


def test_embedder_failure_propagates_rather_than_scoring_zero():
    """模型加载失败若被吞成 0 分，症状是"检索突然什么都召不回"，
    排查方向从一开始就是错的。"""

    class Broken:
        def encode(self, texts):
            raise RuntimeError("模型没加载起来")

    with pytest.raises(RuntimeError):
        VectorScorer(Broken()).relevance("甲", "乙")


def test_threshold_is_configurable_and_defaults_to_a_placeholder():
    """默认值 0.5 是占位，不是调优结果 —— 真值由 eval 的阈值扫描给出。"""
    assert VectorScorer(FakeEmbedder({})).min_relevance == 0.5
    assert VectorScorer(FakeEmbedder({}), min_relevance=0.62).min_relevance == 0.62


def test_encode_calls_counter_tracks_real_encoding_work():
    fake = FakeEmbedder({})
    scorer = VectorScorer(fake)

    scorer.warm(["甲", "乙"])
    scorer.relevance("甲", "乙")
    scorer.relevance("丙", "甲")

    assert scorer.encode_calls == 3
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/serve/test_vector_scoring.py -q`
Expected: FAIL —— `ImportError: cannot import name 'VectorScorer'`

- [ ] **Step 3: 写 `serve/embedding.py`（只放协议，真实现留到 Task 3）**

```python
"""embedding 这一层的边界。

**本模块是整个 serve/ 里唯一允许 import 模型库的地方**（真实现在 Task 3
加进来）。`query.py` 与 `scoring.py` 只认下面这个协议，因此领域层的
55 条测试可以注入假 embedder，零依赖、零下载、毫秒级跑完 ——
"全可测"这条性质靠这道边界保住。

手法与 `judges/` 完全一致：协议在领域侧，实现在外围，测试注入假的。
"""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """把文本批量编码成向量。

    单方法协议是刻意的：query/document 的不对称（有些模型要求查询侧加
    instruction 前缀）在实现内部消化，不外泄到协议 —— 否则领域层就得
    知道模型的脾气，这层边界就白划了。
    """

    def encode(self, texts: list[str]) -> list[list[float]]: ...
```

- [ ] **Step 4: 在 `serve/scoring.py` 追加 `VectorScorer`**

```python
def _cosine(a: Sequence[float], b: Sequence[float]) -> float:
    """余弦相似度，负值截断为 0。

    截断不是洁癖：负相关度会让 `SECONDARY_WEIGHT * secondary` 这种加权
    反向发力，把"明确不相关"排到"完全没关系"前面去。
    """
    if len(a) != len(b):
        raise ValueError(f"向量维度不一致：{len(a)} vs {len(b)}")
    dot = sum(x * y for x, y in zip(a, b))
    norm_a = math.sqrt(sum(x * x for x in a))
    norm_b = math.sqrt(sum(y * y for y in b))
    if norm_a == 0.0 or norm_b == 0.0:
        return 0.0
    return max(0.0, dot / (norm_a * norm_b))


class VectorScorer:
    """向量打分：余弦相似度 + 按文本记忆的向量缓存。

    缓存不是优化，是可行性：语料 ~150 条文本，每次查询要跟其中每一条比
    一遍，不缓存就是每次查询几百次 encode。`warm` 让整个语料一次编码完。

    **默认阈值 0.5 是占位，不是调优结果。** 真值由
    `scripts/eval_diagnosis.py --threshold-sweep` 扫出前沿后，在与字面基线
    相同的工作点（空样本正确率 100%）上读取 —— 而不是挑一个刚好过闸门的数。
    """

    def __init__(self, embedder: Embedder, *, min_relevance: float = 0.5) -> None:
        self._embedder = embedder
        self._cache: dict[str, list[float]] = {}
        self.min_relevance = min_relevance
        self.encode_calls = 0

    def warm(self, texts: Sequence[str]) -> None:
        pending = [t for t in dict.fromkeys(texts) if t.strip() and t not in self._cache]
        if pending:
            self._encode_into_cache(pending)

    def relevance(self, query: str, target: str) -> float:
        if not query.strip() or not target.strip():
            return 0.0
        pending = [t for t in dict.fromkeys((query, target)) if t not in self._cache]
        if pending:
            self._encode_into_cache(pending)
        return _cosine(self._cache[query], self._cache[target])

    def _encode_into_cache(self, texts: list[str]) -> None:
        vectors = self._embedder.encode(texts)
        if len(vectors) != len(texts):
            raise ValueError(f"embedder 返回 {len(vectors)} 条向量，但请求了 {len(texts)} 条")
        self.encode_calls += len(texts)
        self._cache.update(zip(texts, vectors))
```

同时在文件头补 `import math`，并从 `cn_curriculum_graph.serve.embedding` 导入 `Embedder`。

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/serve/test_vector_scoring.py -q`
Expected: PASS（12 passed）

- [ ] **Step 6: 全套回归**

Run: `uv run pytest -q`
Expected: PASS，**381 passed**

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(serve): Embedder 协议 + VectorScorer（余弦 + 向量缓存）

全程假 embedder，不加载任何模型：12 条契约测试覆盖病态输入 ——
空文本不编码、零向量不除零、维度不一致抛 ValueError（确定性错误，
按 error-taxonomy 不可重试）、embedder 抛错必须冒泡而不是吞成 0 分
（吞了的话症状是"检索突然什么都召不回"，排查方向从一开始就是错的）。

缓存不是优化是可行性：语料 150 条、每次查询要跟每条比一遍，
不缓存就是每次查询几百次 encode。

默认阈值 0.5 明确标注为占位值，真值由阈值扫描在与字面基线相同的
工作点上读取。"
```

---

### Task 3: `BGEEmbedder` 真实现 + 可选依赖接线

**Files:**
- Modify: `src/cn_curriculum_graph/serve/embedding.py`
- Modify: `pyproject.toml`
- Test: `tests/serve/test_embedding.py`

**Interfaces:**
- Consumes: `serve.embedding.Embedder`（Task 2）
- Produces:
  - `serve.embedding.BGEEmbedder(model_name: str = "BAAI/bge-m3", *, device: str | None = None)`，实现 `Embedder`
  - `serve.embedding.DEFAULT_MODEL: str = "BAAI/bge-m3"`
  - `serve.embedding.build_embedder(model_name: str | None = None) -> Embedder`（工厂，供 eval 脚本用）

- [ ] **Step 1: 写失败测试（不下载模型）**

创建 `tests/serve/test_embedding.py`：

```python
"""真 embedder 的接线测试 —— 不下载模型。

这层测试只回答一个问题：**没装可选依赖时，报错说不说人话**。
模型质量由 eval 量，模型加载由手动跑一次 eval 验证，都不在这里。
"""

import pytest

from cn_curriculum_graph.serve.embedding import DEFAULT_MODEL, build_embedder


def test_default_model_is_named_explicitly():
    """模型名是实验的一个变量，必须能一眼看到、一行换掉。"""
    assert DEFAULT_MODEL == "BAAI/bge-m3"


def test_missing_optional_dependency_says_how_to_fix_it(monkeypatch):
    """缺可选依赖时报一句 ModuleNotFoundError，用户得自己猜要装什么。
    这里钉住错误消息必须给出安装命令。"""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(ImportError) as excinfo:
        build_embedder()

    assert "uv sync --extra embed" in str(excinfo.value)


def test_build_embedder_accepts_a_model_override():
    """换模型必须是一行 —— 这个实验的第二轮就是换模型再跑一遍。"""
    import inspect

    assert "model_name" in inspect.signature(build_embedder).parameters
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/serve/test_embedding.py -q`
Expected: FAIL —— `ImportError: cannot import name 'DEFAULT_MODEL'`

- [ ] **Step 3: 在 `serve/embedding.py` 追加真实现**

```python
DEFAULT_MODEL = "BAAI/bge-m3"

_MISSING_DEP = (
    "向量检索需要可选依赖，未安装。装它：\n"
    "    uv sync --extra embed\n"
    "（约 GB 级下载；默认的 uv sync 不装它，CI 也不装 —— "
    "领域层的单测靠假 embedder 跑，不需要模型。）"
)


class BGEEmbedder:
    """`sentence-transformers` 的一层薄包装。

    **模型在首次 encode 时才加载，不在构造时**：eval 脚本会先构造再决定
    要不要跑，构造即加载会让 `--help` 都要等几十秒下模型。
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, *, device: str | None = None) -> None:
        self.model_name = model_name
        self._device = device
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            try:
                from sentence_transformers import SentenceTransformer
            except ModuleNotFoundError as exc:  # pragma: no cover - 见 test_embedding.py
                raise ImportError(_MISSING_DEP) from exc
            self._model = SentenceTransformer(self.model_name, device=self._device)
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        model = self._ensure_model()
        return [list(map(float, v)) for v in model.encode(texts, normalize_embeddings=True)]


def build_embedder(model_name: str | None = None) -> Embedder:
    """工厂：eval 脚本唯一的入口。

    这里就地做一次依赖检查，好让"没装依赖"在脚本启动时报出人话，
    而不是等第一次 encode 时抛一句 ModuleNotFoundError。
    """
    try:
        import sentence_transformers  # noqa: F401
    except ModuleNotFoundError as exc:
        raise ImportError(_MISSING_DEP) from exc
    return BGEEmbedder(model_name or DEFAULT_MODEL)
```

- [ ] **Step 4: 在 `pyproject.toml` 加可选依赖**

在 `[project]` 段之后追加：

```toml
[project.optional-dependencies]
# 向量检索实验用。**刻意不进主依赖**：GB 级下载会让 uv sync 从秒级变成
# 分钟级，而领域层的全部单测靠假 embedder 跑，不需要它。
# 装法：uv sync --extra embed
embed = [
    "sentence-transformers>=3.0",
]
```

- [ ] **Step 5: 运行测试确认通过**

Run: `uv run pytest tests/serve/test_embedding.py -q`
Expected: PASS（3 passed）

- [ ] **Step 6: 确认默认环境仍然干净**

Run: `uv sync && uv run python -c "import importlib.util; print('sentence_transformers 已装' if importlib.util.find_spec('sentence_transformers') else 'sentence_transformers 未装（符合预期）')"`
Expected: 输出 `sentence_transformers 未装（符合预期）`

Run: `uv run pytest -q`
Expected: PASS，**384 passed**

- [ ] **Step 7: 提交**

```bash
git add -A
git commit -m "feat(serve): BGEEmbedder + embed 可选依赖

模型在首次 encode 时才加载，不在构造时 —— 否则连 --help 都要先等
几十秒下模型。缺依赖时报的是带安装命令的人话，不是一句
ModuleNotFoundError。

sentence-transformers 进 [project.optional-dependencies]，默认
uv sync 不装、CI 不装：领域层全部单测靠假 embedder 跑，不需要模型。"
```

---

### Task 4: eval 脚本支持两条路线与阈值扫描

**Files:**
- Modify: `scripts/eval_diagnosis.py`
- Test: `tests/test_eval_diagnosis.py`

**Interfaces:**
- Consumes: `serve.scoring.LiteralScorer` / `VectorScorer`（Task 1、2）、`serve.embedding.build_embedder`（Task 3）、现有 `run_cases` / `recall_at` / `empty_case_accuracy`
- Produces:
  - `eval_diagnosis.FrontierPoint`（dataclass）：`threshold: float`、`recall_at_3: float`、`empty_accuracy: float`
  - `eval_diagnosis.sweep_thresholds(index, cases, scorer, thresholds: Sequence[float], *, limit: int = 5) -> list[FrontierPoint]`
  - `eval_diagnosis.same_operating_point(frontier: Sequence[FrontierPoint], *, empty_accuracy: float = 1.0) -> FrontierPoint | None`
  - CLI 新增：`--scorer {literal,vector}`、`--model <name>`、`--threshold-sweep`

- [ ] **Step 1: 写失败测试**

追加到 `tests/test_eval_diagnosis.py`：

```python
# --- 阈值前沿 ---------------------------------------------------------


def test_sweep_returns_one_point_per_threshold():
    from eval_diagnosis import sweep_thresholds

    from cn_curriculum_graph.models import Misconception
    from cn_curriculum_graph.serve.query import GraphIndex
    from cn_curriculum_graph.serve.scoring import LiteralScorer
    from conftest import graph, topic

    index = GraphIndex(
        graph(topics=[topic("A", misconceptions=[
            Misconception(statement="1/8 比 1/5 大", probe="哪个大？", correction_hint="份数越多每份越小"),
        ])])
    )
    cases = [{"id": "c1", "observation": "孩子坚持八分之一比五分之一大", "expected_topic_ids": ["A"]}]

    frontier = sweep_thresholds(index, cases, LiteralScorer(), [0.1, 0.3, 0.9])

    assert [p.threshold for p in frontier] == [0.1, 0.3, 0.9]
    assert frontier[0].recall_at_3 == 1.0
    assert frontier[-1].recall_at_3 == 0.0


def test_same_operating_point_picks_the_best_recall_at_full_empty_accuracy():
    """公平性的全部保障：两条路线必须在同一个工作点上比。"""
    from eval_diagnosis import FrontierPoint, same_operating_point

    frontier = [
        FrontierPoint(threshold=0.1, recall_at_3=0.95, empty_accuracy=0.33),
        FrontierPoint(threshold=0.3, recall_at_3=0.89, empty_accuracy=1.0),
        FrontierPoint(threshold=0.5, recall_at_3=0.74, empty_accuracy=1.0),
    ]

    best = same_operating_point(frontier)

    assert best.threshold == 0.3
    assert best.recall_at_3 == 0.89


def test_same_operating_point_returns_none_when_nothing_reaches_it():
    """报 None 而不是退而求其次挑一个 —— 悄悄换工作点正是这个实验
    最容易自欺的地方。"""
    from eval_diagnosis import FrontierPoint, same_operating_point

    frontier = [FrontierPoint(threshold=0.1, recall_at_3=0.95, empty_accuracy=0.66)]

    assert same_operating_point(frontier) is None
```

- [ ] **Step 2: 运行测试确认失败**

Run: `uv run pytest tests/test_eval_diagnosis.py -q`
Expected: FAIL —— `ImportError: cannot import name 'sweep_thresholds'`

- [ ] **Step 3: 在 `scripts/eval_diagnosis.py` 实现前沿计算**

```python
@dataclass
class FrontierPoint:
    threshold: float
    recall_at_3: float
    empty_accuracy: float


def sweep_thresholds(index, cases, scorer, thresholds, *, limit: int = 5) -> list[FrontierPoint]:
    """同一个打分器、同一份语料，只挪阈值。

    复用同一个 scorer 实例是**必须的**：向量打分器的缓存让整轮扫描只编码
    一次语料。每个阈值新建一个 scorer 会把扫描变成 N 倍的模型调用。
    """
    points = []
    original = scorer.min_relevance
    try:
        for threshold in thresholds:
            scorer.min_relevance = threshold
            results = run_cases(index, cases, limit=limit)
            points.append(
                FrontierPoint(
                    threshold=threshold,
                    recall_at_3=recall_at(results, 3),
                    empty_accuracy=empty_case_accuracy(results),
                )
            )
    finally:
        scorer.min_relevance = original
    return points


def same_operating_point(frontier, *, empty_accuracy: float = 1.0) -> FrontierPoint | None:
    """在"空样本正确率达到给定水平"的点里取召回最高的那个。

    **两条路线必须在同一个工作点上比较**，否则就是拿"向量在 A 点"对
    "字面在 B 点"，怎么比都能比赢。达不到该工作点时返回 None 而不是
    退而求其次 —— 悄悄换工作点正是这个实验最容易自欺的地方。
    """
    qualified = [p for p in frontier if p.empty_accuracy >= empty_accuracy]
    return max(qualified, key=lambda p: p.recall_at_3) if qualified else None
```

- [ ] **Step 4: 运行测试确认通过**

Run: `uv run pytest tests/test_eval_diagnosis.py -q`
Expected: PASS（17 passed）

- [ ] **Step 5: 接 CLI**

在 `main()` 里加参数并构造对应打分器：

```python
    parser.add_argument("--scorer", choices=("literal", "vector"), default="literal")
    parser.add_argument("--model", default=None, help="向量打分器用哪个模型（默认 BAAI/bge-m3）")
    parser.add_argument("--threshold-sweep", action="store_true", help="扫描阈值并打印前沿曲线")
    parser.add_argument(
        "--sweep-from", type=float, default=0.05, help="扫描起点")
    parser.add_argument("--sweep-to", type=float, default=0.95, help="扫描终点")
    parser.add_argument("--sweep-step", type=float, default=0.05, help="扫描步长")
```

构造打分器（`build_embedder` 只在选了 vector 时才调用，`literal` 路径不碰可选依赖）：

```python
    if args.scorer == "vector":
        from cn_curriculum_graph.serve.embedding import build_embedder

        scorer = VectorScorer(build_embedder(args.model))
    else:
        scorer = LiteralScorer()

    started = time.perf_counter()
    index = GraphIndex(load_graph(args.graph), scorer=scorer)
    warm_seconds = time.perf_counter() - started
```

扫描分支打印前沿表与同工作点结果：

```python
    if args.threshold_sweep:
        thresholds = [
            round(args.sweep_from + i * args.sweep_step, 4)
            for i in range(int((args.sweep_to - args.sweep_from) / args.sweep_step) + 1)
        ]
        frontier = sweep_thresholds(index, cases, scorer, thresholds, limit=args.limit)
        print(f"{'阈值':<8}{'recall@3':<12}空样本正确率")
        print("-" * 40)
        for point in frontier:
            print(f"{point.threshold:<8}{point.recall_at_3:<12.0%}{point.empty_accuracy:.0%}")

        best = same_operating_point(frontier)
        print("\n同工作点（空样本正确率 = 100%）：")
        if best is None:
            print("  ✗ 该打分器在扫描范围内**从未**达到 100% 空样本正确率 ——")
            print("    它拿不到与字面基线同台的资格，不能只报它召回高的那个点。")
            return 1
        print(f"  阈值 {best.threshold} → recall@3 = {best.recall_at_3:.0%}")
        print(f"  （字面基线在同一工作点上是 84%）")
        print(f"\n代价：建索引 {warm_seconds:.1f}s，编码 {getattr(scorer, 'encode_calls', 0)} 条文本")
        return 0
```

- [ ] **Step 6: 确认字面路径行为未变**

Run: `uv run python scripts/eval_diagnosis.py`
Expected: `recall@3 = 84%`、空样本 `100%`、退出码 0

Run: `uv run python scripts/eval_diagnosis.py --threshold-sweep`
Expected: 打印前沿表；同工作点一行显示 `阈值 0.2 → recall@3 = 84%` 附近的结果（字面版在 0.15 上可能更高，如实打印即可）

- [ ] **Step 7: 全套回归**

Run: `uv run pytest -q`
Expected: PASS，**387 passed**

- [ ] **Step 8: 提交**

```bash
git add -A
git commit -m "feat(eval): 阈值前沿扫描 + 同工作点对比 + --scorer 开关

同工作点比较是这个实验公平性的全部保障：两条路线都在"空样本正确率
=100%"上报 recall@3，否则就是拿"向量在 A 点"对"字面在 B 点"。
达不到该工作点时返回 None 并非零退出，而不是退而求其次挑一个 ——
悄悄换工作点正是最容易自欺的地方。

扫描全程复用同一个 scorer 实例：向量版的缓存让整轮扫描只编码一次语料，
每个阈值新建实例会把扫描变成 N 倍模型调用。"
```

---

### Task 5: 跑真实验、量代价、写对比笔记

**Files:**
- Create: `practice/cn-curriculum-graph/docs/rag-vs-literal.md`
- Modify: `practice/cn-curriculum-graph/README.md`
- Modify: `memory/learning-log.md`
- Modify: `memory/decisions.md`

**Interfaces:**
- Consumes: Task 4 的 CLI

- [ ] **Step 1: 装可选依赖并记录代价**

```bash
cd practice/cn-curriculum-graph
du -sh .venv                          # 装之前
time uv sync --extra embed
du -sh .venv                          # 装之后
```
把两次 `du` 的差值与 `uv sync` 耗时记下来 —— 这是"依赖增量"这项代价。

- [ ] **Step 2: 跑字面基线的完整前沿（留档）**

```bash
uv run python scripts/eval_diagnosis.py --scorer literal --threshold-sweep | tee /tmp/frontier-literal.txt
```

- [ ] **Step 3: 跑向量版的完整前沿**

```bash
time uv run python scripts/eval_diagnosis.py --scorer vector --threshold-sweep | tee /tmp/frontier-vector.txt
```
第一次会下模型，记下**模型体积**（`du -sh ~/.cache/huggingface/hub`）与**首次运行总耗时**；再跑第二次记**热启动耗时**，两者差值即模型加载成本。

- [ ] **Step 4: 量单次查询延迟**

```bash
uv run python - <<'PY'
import time, json, sys
sys.path.insert(0, "src")
from pathlib import Path
from cn_curriculum_graph.serve.query import GraphIndex, load_graph, match_misconceptions
from cn_curriculum_graph.serve.scoring import LiteralScorer, VectorScorer
from cn_curriculum_graph.serve.embedding import build_embedder

graph = load_graph(Path("data/generated/graph.json"))
cases = json.load(open("data/diagnosis-eval-groundtruth.json"))["cases"]

for name, scorer in [("literal", LiteralScorer()), ("vector", VectorScorer(build_embedder()))]:
    idx = GraphIndex(graph, scorer=scorer)
    t0 = time.perf_counter()
    for c in cases:
        match_misconceptions(idx, c["observation"], limit=5)
    elapsed = (time.perf_counter() - t0) / len(cases)
    print(f"{name}: 单次查询 {elapsed*1000:.1f}ms（语料已热）")
PY
```

- [ ] **Step 5: 写 `docs/rag-vs-literal.md`**

结构固定为下面六节，**第 5、6 节不许省略**：

```markdown
# 字面匹配 vs 向量检索：一次受控对比

> 时间：2026-07-27。模型：BAAI/bge-m3（本地，Apache 2.0 生态）。
> 设计与判据见 docs/superpowers/specs/2026-07-27-rag-vs-literal-design.md。

## 1. 实验怎么设计的（只动一个变量）
## 2. 完整阈值前沿（两条曲线并排）
## 3. 同工作点结果（空样本正确率 = 100%）
## 4. 逐条看未命中样本的变化：字面赢在哪、向量赢在哪
## 5. 代价（四项，全部实测数字）
   - 模型体积 / uv sync 增量 / 建索引耗时 / 单次查询延迟
## 6. 诚实结论
   - 明确回答："纯检索够不够用"这个问题第二次被数字回答的结果是什么
   - 若向量明显更好 → ADR-0006 该被推翻并重写，写清楚怎么改
   - 若差不多或更差 → 这是"简单方案已经够用"的硬证据，同样写清楚
   - recall@1 与 recall@3 是否终于拉开差距（字面版三者相等，排序维度是空的）
   - 22 条样本的统计精度限制：±1 条命中 ≈ ±5 个百分点，别把 84% vs 89% 讲成显著提升
```

- [ ] **Step 6: 更新 README 与 memory**

- README「怎么跑」补一段向量检索的跑法与 `--extra embed` 说明；「下一步」列表按结果更新。
- `memory/learning-log.md` 顶部加一条（日期 + 一句话结论 + 代价数字 + 下一步）。
- `memory/decisions.md`：若向量明显更好，新增 ADR-0007 并在 ADR-0006 标注「已被 ADR-0007 取代」；若不是，在 ADR-0006 追加一条「2026-07-27 向量对比补记」。**两种情况都要写**，不能只在赢了的时候记。

- [ ] **Step 7: 全套回归 + 确认默认环境仍不需要模型**

```bash
uv sync                 # 回到不带 extra 的环境
uv run pytest -q        # 必须仍然全绿
```
Expected: PASS，**387 passed**（可选依赖卸掉后单测不受影响 —— 这是"全可测"这条性质的最终验证）

- [ ] **Step 8: 提交并推送**

```bash
git add -A
git commit -m "docs(rag): 字面 vs 向量受控对比实测结果与代价核算"
git push
```

---

## Self-Review

**Spec 覆盖检查：** §1 事实核实 → 已写入 spec，无需任务；§2 本地模型选型 → Task 3；§3 分层与依赖注入 → Task 1、2、3；§4 只动一个变量 → Task 1 Step 5 + Global Constraints；§5 阈值与同工作点 → Task 4；§6 明确不做 → Global Constraints 与各任务范围（无向量库/无 chunking/无 rerank/无 hybrid）；§7 测试策略 → Task 1、2、3 的测试步骤 + Task 5 Step 7；§8 验收判据四项 → Task 5 Step 1–5；§9 不确定性 → Task 5 Step 5 第 6 节。**无缺口。**

**占位符扫描：** 无 TBD / TODO；每个代码步骤都带可直接粘贴的代码；每个运行步骤都带确切命令与预期输出。

**类型一致性：** `Scorer.min_relevance` / `warm` / `relevance` 在 Task 1 定义，Task 2 的 `VectorScorer` 与 Task 4 的 `sweep_thresholds` 用的是同一组名字；`Embedder.encode` 在 Task 2 定义、Task 3 实现；`FrontierPoint` 三个字段在 Task 4 内自洽。
