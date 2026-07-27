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

    这是 2026-07-27 那轮的产物，recall@3 = 84%（空样本正确率 100%）。
    它是这次对比的基线，行为不许在重构里发生任何改变。
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
    # **不是调这个数**。扫过一遍全档（见 memory）：任何单纯调阈值的做法都只是
    # 在"召得回"与"敢返回空"之间沿着同一条前沿滑动，换不来净收益。
    min_relevance = 0.2

    def warm(self, texts: Sequence[str]) -> None:
        """字面打分无需预热。协议要求方法存在，向量版才用得上。"""

    def relevance(self, query: str, target: str) -> float:
        return _relevance(_grams(query), _grams(target))
