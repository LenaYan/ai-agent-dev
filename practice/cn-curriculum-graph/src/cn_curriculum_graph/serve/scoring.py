"""打分器：把"两段文本有多相关"抽成可替换的一层。

**为什么阈值也挂在打分器上**：余弦相似度与字面覆盖率的标度完全不同 ——
0.2 在覆盖率上是"敢返回空"的门槛，在余弦上什么都不是。阈值是打分器的
性质，不是检索的性质。

**为什么聚合权重不在这里**（`DESCRIPTION_WEIGHT` / `SECONDARY_WEIGHT` 留在
`query.py`）：那两个数在两条路线上必须完全一致，否则跑出来的差值说不清是
打分函数的功劳还是聚合的功劳。**只动一个变量**是这次对比的全部前提。
"""

from __future__ import annotations

import math
import re
from typing import Protocol, Sequence

from cn_curriculum_graph.serve.embedding import Embedder

_CN_DIGITS = {c: str(i) for i, c in enumerate("零一二三四五六七八九")} | {"十": "10"}
_OPERATORS = {"加": "+", "减": "-", "乘": "×", "除以": "÷"}
_NUMBER = r"(?:\d+|[零一二三四五六七八九十])"


def _as_digits(token: str) -> str:
    return _CN_DIGITS.get(token, token)


def normalize_math(text: str) -> str:
    """把口语的数学表达改写成符号写法。

    **为什么需要这一步**（2026-07-27 评测逼出来的，不是设计时想到的）：
    图里的误概念是用符号写的（`1/4比1/3大`、`5-3=3-5`），而家长是用嘴说的
    （"八分之一比五分之一大"、"5 减 3"）。这两种写法的字面重叠是**零**
    ——不是同义词问题，是同一个数学对象的两种表示。加权、调阈值都救不了，
    归一化两边即可，仍然不需要模型。

    只在**数字之间**改写运算符：`5 减 3` → `5-3`，但"减法也有交换律"
    原样不动。否则"减法"会变成"-法"，伤到一大批正常词。
    """
    text = re.sub(
        rf"({_NUMBER})分之({_NUMBER})",
        lambda m: f"{_as_digits(m.group(2))}/{_as_digits(m.group(1))}",
        text,
    )
    return re.sub(
        rf"({_NUMBER})\s*({'|'.join(_OPERATORS)})\s*({_NUMBER})",
        lambda m: f"{_as_digits(m.group(1))}{_OPERATORS[m.group(2)]}{_as_digits(m.group(3))}",
        text,
    )


def _normalize(text: str) -> str:
    return "".join(ch for ch in normalize_math(text).lower() if not ch.isspace())


def _grams(text: str) -> frozenset[str]:
    """字符二元组。

    中文没有空格分词，而引入 jieba 这类分词器会带来一个模型级别的依赖
    （词典 + 新词发现），对"给 LLM 出候选"这个用途是过度工程。
    二元组的性质刚好合用：`"小数"` 这种查询精确命中，
    `"光合作用"` 与数学节点重叠为零（见 `test_search_returns_empty_rather_than_forcing_a_hit`）。
    """
    s = _normalize(text)
    if len(s) < 2:
        return frozenset([s]) if s else frozenset()
    return frozenset(s[i : i + 2] for i in range(len(s) - 1))


def _relevance(query_grams: frozenset[str], target: frozenset[str]) -> float:
    """两个方向的覆盖率取大者。

    **为什么不只看"查询被覆盖了多少"**（首版就是那样，实测 recall@3 卡在
    63%）：家长转述一句话往往比图里的误概念长得多，分母里塞满"他说""一样"
    这类没有信息量的字，把真正对上的"交换/减法"稀释到门槛以下。
    **说得越具体越啰嗦，越被惩罚** —— 这是度量的缺陷，不是字面匹配的极限。

    反方向（这条误概念的特征被观察句覆盖了多少）恰好治这个：短 statement
    被一句长转述覆盖时，得分很高。两个方向取大者，等于承认命中可以来自
    任一侧。

    也试过 IDF 加权，**实测更差**（recall 63% → 53%）：IDF 惩罚的是"在图语料里
    罕见"，而家长口语里的噪声词恰恰在图语料里从没出现过，df=0 → 权重最大，
    把分母顶得更高。方向正好搞反了。这条失败记录保留在这里，别再试第二遍。
    """
    if not query_grams or not target:
        return 0.0
    shared = len(query_grams & target)
    return max(shared / len(query_grams), shared / len(target))


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

    这是 2026-07-27 那轮的产物。**基线数字是 recall@3 = 89%**（阈值 0.15，
    空样本正确率 100%），不是长期被引用的 84% —— 84% 只是生产默认阈值 0.2
    上的成绩，完整前沿扫出来的同工作点最优点在 0.15（见 docs/rag-vs-literal.md §3）。
    引用基线时用 89%：拿 84% 当基线会让任何新方案凭一个偏低的基准"赢"。

    受控对比的结论：同工作点上 **字面 89% vs bge-m3 向量 79%**，且向量的
    命中集是字面命中集的真子集（零个独赢样本）。行为不许在重构里发生任何改变。
    """

    # 入选门槛：相关度必须到这个数。
    #
    # **这道门槛是"敢返回空"的实现**：没有它，一次偶然的字重叠就能凑出候选
    # ——"孩子不爱吃青菜"会命中一条 correction_hint，只因为两边都有"孩子"
    # 二字（`test_match_misconceptions_returns_empty_when_nothing_is_close`）。
    # 门槛卡在**原始相关度**上而不是加权分上：加权只用于排序，不该让
    # description 命中因为折价而掉出候选。
    #
    # 0.2 从首版起没有动过 —— 这一点很重要：2026-07-27 那次评测把 recall@3
    # 从 63% 修到 84%，靠的是改打分公式与归一化（都是结构性缺陷），
    # **不是调这个数**。
    #
    # **一条曾经写在这里、已被证伪的话**（2026-07-27 完整前沿扫描）：原注释说
    # "扫过一遍全档：任何单纯调阈值的做法都只是在'召得回'与'敢返回空'之间沿着
    # 同一条前沿滑动，换不来净收益"。**不成立** —— 阈值 0.15 在评测上同时拿到
    # 89% 召回和 100% 空样本正确率，比这里的 0.2（84%）净高 5 个百分点。
    # 上一轮大概率只扫了 0.1 和 0.2，跳过了 0.15，于是把"我没扫到"当成了
    # "不存在"。完整前沿见 docs/rag-vs-literal.md §2。
    #
    # **2026-07-28：真的把它改成 0.15 试过一次，又改了回来。** 结论不再是
    # "存疑所以先不动"，而是**有证据的不动**：
    #
    #   改成 0.15 后 `test_match_misconceptions_returns_empty_when_nothing_is_close`
    #   立刻变红 —— "孩子不爱吃青菜" 命中了上面那条 correction_hint
    #   （"引导**孩子**对齐数位…"），相关度 **0.1667**，正好卡在 0.15 与 0.2
    #   之间。查询 7 个字 = 6 个 bigram，只有"孩子"这一个命中，1/6 = 0.1667。
    #   **这正是本注释开头描述的那个失败模式**：一次偶然的字重叠凑出候选。
    #
    # 而评测在 0.15 上报的空样本正确率仍是 100% —— 因为 ground truth 里只有
    # **3 条**空样本，太少，覆盖不到这个模式。**"前沿上 100%" 与 "这道门槛
    # 真的守得住" 不是一回事**，这次实测就是它们分开的地方。
    #
    # 所以这 5 个百分点的账是：收益 = 1 条样本（n=19，±1 条 ≈ ±5.3pp，
    # 统计上与 0.2 区分不开），代价 = 一个被实际演示出来的误招模式。不划算。
    #
    # **2026-07-28 当天晚些：空样本从 3 条补到 11 条，上面那笔账有一半塌了。**
    # 在 11 条空样本下 0.15 与 0.2 的空样本正确率**一样**（都是 91%，10/11），
    # 而 0.15 的 recall@3 高 5 个百分点 —— 也就是说"0.15 会多漏"这条理由在
    # ground truth 上**不成立**，它只在上面那条单测 fixture 上成立，而那条
    # 样本不在 ground truth 里。**决定是对的，理由是错的，两者要分开记账。**
    #
    # 更重要的是问题本身换了：0.15 和 0.2 在 11 条空样本下**都够不到 100%**，
    # 两个都不是工作点。真正的工作点右移到 **0.25**（79% 召回 / 11 条全对）。
    #
    # **于是真正的问题是 0.2 vs 0.25，而这个决定已经做了：留在 0.2。**
    # 这是一个**明知代价的选择**，代价必须写在这里而不是藏在文档里：
    #
    #   0.2 上有一条**已知误招** —— ground truth 的 `empty_bar_chart`
    #   （"看条形统计图……读不出具体的数量"）会误召「用字母表示关系」的
    #   statement「字母只能代表一个具体的数，不能代表变化的量」，相关度
    #   **0.2105**，重叠的是"具体的数"这类通用词。抬到 0.25 能把空样本正确率
    #   做到 11/11，代价是 recall@3 84% → 79%（掉的两条是从命中变成空手而归，
    #   不是变成误召）。两边都不是白拿 —— 这次没有"免费的 5 个百分点"可捡。
    #
    # **什么情况下该重开这个决定**：11 条空样本**刻意排除**了"领域内但图没覆盖"
    # 与"说的是对的"两类，所以这道门槛目前只被"完全不沾边"的样本压过，
    # **还没被"沾边但不该召回"压过 —— 而后者才是生产里更常见的一类**。
    # 补上那一类样本后若 0.2 的误招不止这一条，这个决定就该重新算。
    # 完整权衡见 docs/rag-vs-literal-backlog.md。
    min_relevance = 0.2

    def warm(self, texts: Sequence[str]) -> None:
        """字面打分无需预热。协议要求方法存在，向量版才用得上。"""

    def relevance(self, query: str, target: str) -> float:
        return _relevance(_grams(query), _grams(target))


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

    缓存不是优化，是可行性：语料有数百条文本（实测规模见
    `docs/rag-vs-literal.md`），每次查询要跟其中每一条比一遍，不缓存就是
    每次查询几百次 encode。`warm` 让整个语料一次编码完。

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
