"""诊断检索评测的契约测试 —— 不触网、不需要 key（领域层本来就没有 LLM）。

两类测试，第二类才是重点：

1. 统计口径对不对（recall@k 怎么算、空样本怎么算、闸门什么时候红）。
2. **ground truth 本身可不可信**：id 是不是真的存在于图里、样本量够不够、
   有没有"应当召回不到"的样本、以及**观察句是不是抄了图里的原文**。
   最后一条是这套评测的命门 —— 抄原文的评测必然高分，而高分什么也不说明。
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))

from eval_diagnosis import (  # noqa: E402
    GROUNDTRUTH,
    RECALL_AT_3_THRESHOLD,
    CaseResult,
    empty_case_accuracy,
    load_cases,
    recall_at,
    verdict,
)

from cn_curriculum_graph.models import Misconception  # noqa: E402
from cn_curriculum_graph.serve.query import GraphIndex, match_misconceptions  # noqa: E402
from conftest import graph, topic  # noqa: E402


def _result(expected: list[str], returned: list[str]) -> CaseResult:
    return CaseResult(case_id="c", observation="o", expected=expected, returned=returned)


# --- 统计口径 -----------------------------------------------------------


def test_recall_counts_a_case_as_hit_when_any_expected_id_is_in_top_k():
    """多个 expected 时命中任意一条即算召回：消费端是 LLM，
    它拿到一条对得上的误概念就能往下推理。"""
    results = [_result(["A", "B"], ["X", "B", "Y"])]

    assert recall_at(results, 3) == 1.0
    assert recall_at(results, 1) == 0.0


def test_recall_ignores_hits_below_the_cutoff():
    results = [_result(["A"], ["X", "Y", "Z", "A"])]

    assert recall_at(results, 3) == 0.0
    assert recall_at(results, 5) == 1.0


def test_recall_excludes_the_should_return_nothing_cases():
    """空样本进 recall 的分母会把指标搅浑：它们量的是"敢不敢返回空"，
    不是"召不召得回"。"""
    results = [_result(["A"], ["A"]), _result([], [])]

    assert recall_at(results, 3) == 1.0


def test_empty_case_accuracy_only_counts_empty_expectations():
    results = [_result([], []), _result([], ["X"]), _result(["A"], ["A"])]

    assert empty_case_accuracy(results) == 0.5


def test_verdict_is_red_below_threshold_and_green_at_it():
    below = [_result(["A"], ["X"])] * 3 + [_result(["A"], ["A"])]  # recall@3 = 0.25
    at = [_result(["A"], ["A"])] * 3 + [_result(["A"], ["X"])]  # recall@3 = 0.75

    assert verdict(below) == 1
    assert verdict(at) == 0
    assert RECALL_AT_3_THRESHOLD == 0.75


def test_verdict_does_not_go_red_on_empty_case_misses_alone():
    """空样本失手记录但不红：硬凑出来的候选，消费端 LLM 有机会自己丢掉；
    而闸门一旦为它变红，就会逼着调高 MIN_COVERAGE，代价是掉真实召回。
    这条是刻意的取舍，不是遗漏。"""
    results = [_result(["A"], ["A"])] * 4 + [_result([], ["X"])]

    assert verdict(results) == 0


# --- ground truth 本身 --------------------------------------------------


def _cases():
    return load_cases(GROUNDTRUTH)


def test_groundtruth_has_at_least_sixteen_cases():
    assert len(_cases()) >= 16


def test_groundtruth_contains_should_return_nothing_cases():
    assert sum(1 for c in _cases() if not c["expected_topic_ids"]) >= 1


def test_every_expected_id_exists_in_the_generated_graph():
    """id 打错字会让评测悄悄变成"永远召不回"，而症状看起来像检索差。"""
    from cn_curriculum_graph.serve.query import load_graph

    graph_path = Path(__file__).resolve().parents[1] / "data" / "generated" / "graph.json"
    known = {t.id for t in load_graph(graph_path).topics}

    missing = [
        (c["id"], tid) for c in _cases() for tid in c["expected_topic_ids"] if tid not in known
    ]
    assert missing == []


def test_observations_are_not_copied_from_the_graph():
    """评测的命门：观察句抄了 misconception 原文，分数就是自己发给自己的。"""
    graph_path = Path(__file__).resolve().parents[1] / "data" / "generated" / "graph.json"
    raw = json.loads(graph_path.read_text(encoding="utf-8"))
    statements = [
        m["statement"] for t in raw["topics"] for m in t["misconceptions"]
    ] + [m["probe"] for t in raw["topics"] for m in t["misconceptions"]]

    copied = [
        c["id"]
        for c in _cases()
        for s in statements
        if c["observation"] in s or s in c["observation"]
    ]
    assert copied == []


# --- 与领域层真的接上了 --------------------------------------------------


def test_a_case_runs_end_to_end_against_a_hand_built_graph():
    """统计口径测完还要证明它接的是真的 match_misconceptions，
    不是一个只在测试里成立的桩。"""
    index = GraphIndex(
        graph(
            topics=[
                topic(
                    "A",
                    misconceptions=[
                        Misconception(
                            statement="0.3 比 0.03 小，因为 3 比 30 小",
                            probe="0.3 和 0.03 哪个大？",
                            correction_hint="关注数位：0.3 是 3 个 0.1。",
                        )
                    ],
                )
            ]
        )
    )

    hits = match_misconceptions(index, "他说 0.3 比 0.03 小", limit=3)

    assert [h.topic_id for h in hits] == ["A"]


@pytest.mark.parametrize("k", [1, 3, 5])
def test_recall_of_an_empty_result_set_is_zero_not_a_crash(k):
    assert recall_at([_result(["A"], [])], k) == 0.0
