"""打分器这一层的契约测试。

这一层存在的理由只有一个：让"两段文本有多相关"可替换，而聚合逻辑不动。
所以这里测的是**协议契约**（阈值、warm、relevance 的取值范围），
不测检索效果 —— 那是 eval 的事。
"""

from cn_curriculum_graph.serve.scoring import LiteralScorer, normalize_math


def test_literal_scorer_carries_its_own_threshold():
    """阈值是打分器的性质，不是检索的性质：余弦与覆盖率的标度完全不同，
    同一个 0.2 在两者上不是一回事。

    2026-07-28 试过改成 0.15（评测上 +5pp 召回且空样本仍 100%），因为它放进来
    一个偶然字重叠的误招而改回 —— 详见 `LiteralScorer.min_relevance` 上方注释，
    以及 `test_match_misconceptions_returns_empty_when_nothing_is_close`。"""
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
