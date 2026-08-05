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


def test_surrounding_whitespace_shares_one_cache_entry():
    """`"甲"` 与 `" 甲 "` 是同一段文本，不该各编码一次。

    修之前它们各占一条缓存 —— 对 2026-07-27 那轮结论零影响（两条路线看到的
    是同一份文本），纯属浪费。
    """
    fake = FakeEmbedder({"甲": [1.0, 0.0], "乙": [1.0, 0.0]})
    scorer = VectorScorer(fake)

    scorer.warm(["甲", " 甲 ", "甲\n"])
    assert scorer.encode_calls == 1

    assert scorer.relevance(" 甲 ", "\t甲") == pytest.approx(1.0)
    assert scorer.encode_calls == 1  # 仍然没有新的编码


def test_cache_key_and_encoded_text_are_the_same_string():
    """真正危险的那个不变量：**只 strip key、却把原文送去 encode**，会让同一个
    key 对应两个不同的向量，比不 strip 更糟 —— 那是静默的错误结果。

    这条测试通过"送进 embedder 的每一段文本自己就是缓存键"来钉住它：
    假 embedder 记录了每次收到的文本，逐条比对。
    """
    fake = FakeEmbedder({"甲": [1.0, 0.0]})
    scorer = VectorScorer(fake)

    scorer.warm([" 甲 ", "乙\n"])
    scorer.relevance("  丙", "甲  ")

    sent = [t for call in fake.calls for t in call]
    assert sent == [t.strip() for t in sent]  # 送去编码的都是已 strip 的
    assert set(sent) == set(scorer._cache)     # 且与缓存键一一对应


def test_whitespace_only_text_still_scores_zero_without_encoding():
    """全空白等于没有文本 —— 不该编码，也不该把零向量塞进缓存。"""
    fake = FakeEmbedder({"甲": [1.0, 0.0]})
    scorer = VectorScorer(fake)

    assert scorer.relevance("   ", "甲") == 0.0
    assert scorer.encode_calls == 0

    scorer.warm(["  ", "\n", ""])
    assert scorer.encode_calls == 0
