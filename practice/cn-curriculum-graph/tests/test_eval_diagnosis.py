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


# --- 阈值前沿 ---------------------------------------------------------


def test_sweep_returns_one_point_per_threshold():
    from eval_diagnosis import sweep_thresholds

    from cn_curriculum_graph.models import Misconception
    from cn_curriculum_graph.serve.query import GraphIndex
    from cn_curriculum_graph.serve.scoring import LiteralScorer
    from conftest import graph, topic

    # sweep_thresholds 只认 index.scorer（不接受调用方另传一个 scorer 实例，
    # 见 sweep_thresholds 自己的 docstring）——这里的 scorer 只是为了在
    # GraphIndex 构造时传入同一个对象，不再作为参数喂给 sweep_thresholds。
    #
    # 观察句不能让 statement 整段被当作连续子串包住 —— LiteralScorer._relevance
    # 取双向覆盖率的大者，一旦 statement 的全部二元组都出现在 query 里，
    # 反方向覆盖率恒为 1.0，任何 <=1.0 的阈值都筛不掉它，阈值扫描同样测不出
    # 效果（brief 原文的观察句就踩了这个坑）。
    scorer = LiteralScorer()
    index = GraphIndex(
        graph(topics=[topic("A", misconceptions=[
            Misconception(statement="1/8 比 1/5 大", probe="哪个大？", correction_hint="份数越多每份越小"),
        ])]),
        scorer=scorer,
    )
    cases = [{"id": "c1", "observation": "孩子坚持八分之一比五分之一还要大", "expected_topic_ids": ["A"]}]

    frontier = sweep_thresholds(index, cases, [0.1, 0.3, 0.9])

    assert [p.threshold for p in frontier] == [0.1, 0.3, 0.9]
    assert frontier[0].recall_at_3 == 1.0
    assert frontier[-1].recall_at_3 == 0.0


def test_sweep_thresholds_ignores_whatever_min_relevance_the_scorer_already_has():
    """CLI help 文案承诺"扫描模式下 --min-relevance 无效"——这条契约当前
    成立（sweep_thresholds 对每个阈值都无条件覆写 index.scorer.min_relevance），
    但此前没有测试钉住它。破坏它的症状是"前沿表看起来正常，但工作点是错的"，
    不会报错、也不会让前沿表本身变形，很难在评审里被肉眼发现。

    这里直接验证契约本身：同一份图、同一组阈值，不管调用前 scorer 的
    min_relevance 停在哪个值上，扫出来的前沿必须逐点相同。"""
    from eval_diagnosis import sweep_thresholds

    from cn_curriculum_graph.models import Misconception
    from cn_curriculum_graph.serve.query import GraphIndex
    from cn_curriculum_graph.serve.scoring import LiteralScorer
    from conftest import graph, topic

    g = graph(topics=[topic("A", misconceptions=[
        Misconception(statement="1/8 比 1/5 大", probe="哪个大？", correction_hint="份数越多每份越小"),
    ])])
    cases = [{"id": "c1", "observation": "孩子坚持八分之一比五分之一还要大", "expected_topic_ids": ["A"]}]
    thresholds = [0.1, 0.3, 0.9]

    default_scorer = LiteralScorer()
    default_index = GraphIndex(g, scorer=default_scorer)
    frontier_from_default = sweep_thresholds(default_index, cases, thresholds)

    # 模拟 CLI 里先跑过 --min-relevance 0.9（逐条模式）、扫描模式复用同一个
    # 打分器实例的情形：min_relevance 在调用前已经被覆写过。
    overridden_scorer = LiteralScorer()
    overridden_scorer.min_relevance = 0.9
    overridden_index = GraphIndex(g, scorer=overridden_scorer)
    frontier_from_overridden = sweep_thresholds(overridden_index, cases, thresholds)

    assert frontier_from_default == frontier_from_overridden


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


def test_threshold_range_includes_both_endpoints_with_default_sweep_params():
    """默认扫描参数 0.05/0.95/0.05 下，浮点误差曾让 int() 把终点 0.95
    悄悄截掉（(0.95-0.05)/0.05 在浮点里是 17.999999999999996）。"""
    from eval_diagnosis import threshold_range

    thresholds = threshold_range(0.05, 0.95, 0.05)

    assert thresholds[0] == 0.05
    assert thresholds[-1] == 0.95
    assert len(thresholds) == 19


def test_threshold_range_handles_floating_point_edge_cases():
    """另一组会踩同一个浮点坑的组合：(0.3-0.1)/0.05 在浮点里是
    3.9999999999999996，int() 会截成 4，丢掉终点 0.3。"""
    from eval_diagnosis import threshold_range

    assert threshold_range(0.1, 0.3, 0.05) == [0.1, 0.15, 0.2, 0.25, 0.3]


def test_threshold_range_does_not_overshoot_stop_on_half_step_rounding():
    """round() 相对 int() 新引入的边界行为：比值恰好落在 .5 附近时，
    Python 的银行家舍入会让点数多算一个，导致末点越过 stop ——
    (1.05-0.0)/0.3 在浮点里是 3.5000000000000004，round() 到 4，
    生成的末点是 1.2，超过了用户要求的终点 1.05。"""
    from eval_diagnosis import threshold_range

    thresholds = threshold_range(0.0, 1.05, 0.3)

    assert thresholds == [0.0, 0.3, 0.6, 0.9]
    assert all(t <= 1.05 for t in thresholds)


# --- 字面基线的扫描区间必须与用户的 --sweep-* 解耦 -----------------------


def test_literal_baseline_ignores_any_caller_supplied_range_and_finds_the_true_optimum():
    """回归钉子：字面基线曾经直接复用调用方（--scorer vector 时用户为
    余弦定制）的 thresholds。这份数据上 statement 与观察句的相关度是
    0.833（任何 <=0.833 的阈值都命中），如果用户把 --sweep-from/to 收窄
    到比如 0.85–0.95（模拟为余弦提高分辨率），字面真正的最优点就落在
    区间外，基线会被错算成 recall@3=0%，而不是真正能拿到的 100%。

    修法是 literal_baseline 压根不接受外部区间参数，只认自己固定的
    LITERAL_BASELINE_SWEEP —— 这里验证不管调用方手上还有什么别的窄区间
    在流转，字面基线自己算出来的都是全区间扫描的真正最优点。"""
    from eval_diagnosis import literal_baseline

    from cn_curriculum_graph.models import Misconception
    from conftest import graph, topic

    g = graph(topics=[topic("A", misconceptions=[
        Misconception(statement="1/8 比 1/5 大", probe="哪个大？", correction_hint="份数越多每份越小"),
    ])])
    cases = [{"id": "c1", "observation": "孩子坚持八分之一比五分之一还要大", "expected_topic_ids": ["A"]}]

    best = literal_baseline(g, cases)

    assert best is not None
    assert best.recall_at_3 == 1.0


# --- 逐条模式必须能停在扫描报出的那个工作点上 ---------------------------


def test_min_relevance_option_overrides_the_scorer_default(tmp_path, capsys):
    """`--threshold-sweep` 报出的最优阈值，逐条模式必须也能停在那个点上。

    没有这个开关时，逐条模式恒用打分器的默认阈值（字面 0.2、向量 0.5），
    而两条路线的同工作点分别在 0.15 和 0.7 —— 于是"扫描说 89%/79%"和
    "逐条列表列出的未命中样本"来自**两个不同的工作点**，拿后者去解释前者
    是错的。对比笔记第 4 节整节都建立在这个逐条列表上，所以这个开关是
    结论可追溯性的一部分，不是便利功能。
    """
    import eval_diagnosis

    g = graph(topics=[topic("A", misconceptions=[
        Misconception(statement="1/8 比 1/5 大", probe="哪个大？", correction_hint="份数越多每份越小"),
    ])])
    graph_path = tmp_path / "graph.json"
    graph_path.write_text(g.model_dump_json(), encoding="utf-8")
    gt_path = tmp_path / "gt.json"
    gt_path.write_text(
        json.dumps({"cases": [
            {"id": "c1", "observation": "孩子坚持八分之一比五分之一还要大", "expected_topic_ids": ["A"]}
        ]}),
        encoding="utf-8",
    )

    # 这条样本与 statement 的相关度是 0.833（见上一个测试）：阈值 0.2
    # （LiteralScorer 的默认值）命中，阈值 0.9 筛掉。逐条模式若不认这个
    # 开关，两次跑出来会是同一个结果。
    argv = ["eval_diagnosis.py", "--graph", str(graph_path), "--groundtruth", str(gt_path)]

    sys.argv = argv + ["--min-relevance", "0.9"]
    assert eval_diagnosis.main() == 1
    assert "recall@3 = 0%" in capsys.readouterr().out

    sys.argv = argv + ["--min-relevance", "0.2"]
    assert eval_diagnosis.main() == 0
    assert "recall@3 = 100%" in capsys.readouterr().out


# --- ground truth 的 id 必须还活在图里 -------------------------------------


def test_stale_expected_ids_are_reported_instead_of_counted_as_misses():
    """标签指向图里不存在的节点时，必须报出来，不能算成"未命中"。

    2026-07-28 踩到的真事：重跑流水线后节点 id 变了（id 是内容哈希，描述一改
    就变），24 个 expected id 里 12 个失效。评测照跑不误，报出 recall@3 = 37%
    并建议"回到 §1 重新论证纯检索够不够" —— 而真相是 19 条计召回样本里 7 条的
    期望 id 全部失效、必然算未命中，**recall 上限只有 63%，闸门 75% 结构上
    不可能通过**。这与检索质量毫无关系。

    这是本项目第三次撞上"度量缺陷冒充模型问题"。前两次是长度惩罚（recall 63%）
    和空样本分母只有 3。所以这一条不是防御性编程，是把踩过的坑钉死。
    """
    from eval_diagnosis import stale_expected_ids

    cases = [
        {"id": "c1", "observation": "甲", "expected_topic_ids": ["alive", "dead1"]},
        {"id": "c2", "observation": "乙", "expected_topic_ids": []},
        {"id": "c3", "observation": "丙", "expected_topic_ids": ["dead2"]},
    ]

    stale = stale_expected_ids(cases, {"alive", "other"})

    assert stale == {"c1": ["dead1"], "c3": ["dead2"]}


def test_no_stale_ids_reports_nothing():
    from eval_diagnosis import stale_expected_ids

    cases = [{"id": "c1", "observation": "甲", "expected_topic_ids": ["alive"]}]

    assert stale_expected_ids(cases, {"alive"}) == {}
