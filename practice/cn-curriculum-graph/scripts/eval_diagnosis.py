"""量诊断检索够不够用 —— recall@1/@3/@5，低于阈值非零退出。

与 `eval_judge.py` / `eval_fidelity.py` 同一套约定：**有 ground truth 才敢
说它够用**。区别在于这个脚本不需要任何 key —— 领域层是纯字面检索，
不调模型（为什么不调，见 docs/mcp-server-design.md §1）。

## 为什么阈值定在 recall@3 而不是 @1

消费端是 LLM，给它三个候选自己挑是合理用法。强求 @1 是把检索工具当成
分类器用，与"推理留给 agent"的设计前提直接矛盾。

## 这个阈值是判据，不是目标

跑完若明显偏低，结论**不是**"再调调 MIN_COVERAGE"，而是回到 §1 重新论证
纯检索够不够、要不要引入 LLM 语义匹配。用数据推翻自己的设计决定，
比守着它强。

跑法（无需 key）：

    uv run python scripts/eval_diagnosis.py
    uv run python scripts/eval_diagnosis.py --graph data/generated/graph.json --limit 5
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cn_curriculum_graph.serve.query import (  # noqa: E402
    GraphIndex,
    load_graph,
    match_misconceptions,
)
from cn_curriculum_graph.serve.scoring import LiteralScorer, VectorScorer  # noqa: E402
from cn_curriculum_graph.serve.embedding import DEFAULT_MODEL  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
GROUNDTRUTH = ROOT / "data" / "diagnosis-eval-groundtruth.json"
DEFAULT_GRAPH = ROOT / "data" / "generated" / "graph.json"

# 阈值：@3 命中率 75%。论证见模块文档。
RECALL_AT_3_THRESHOLD = 0.75


@dataclass
class CaseResult:
    case_id: str
    observation: str
    expected: list[str]
    returned: list[str]
    probes: str = ""
    hit_names: list[str] = field(default_factory=list)

    @property
    def is_empty_case(self) -> bool:
        return not self.expected


def load_cases(path: Path) -> list[dict]:
    return json.loads(path.read_text(encoding="utf-8"))["cases"]


def run_cases(index: GraphIndex, cases: list[dict], *, limit: int = 5) -> list[CaseResult]:
    results = []
    for case in cases:
        hits = match_misconceptions(index, case["observation"], limit=limit)
        results.append(
            CaseResult(
                case_id=case["id"],
                observation=case["observation"],
                expected=list(case["expected_topic_ids"]),
                returned=[h.topic_id for h in hits],
                probes=case.get("probes", ""),
                hit_names=[h.topic_name for h in hits],
            )
        )
    return results


def recall_at(results: list[CaseResult], k: int) -> float:
    """命中率：expected 里任意一条落在 top-k 即算命中。

    空样本（expected 为空）不进分母 —— 它们量的是"敢不敢返回空"，
    不是"召不召得回"，混在一起会把两件事都说不清。
    """
    scored = [r for r in results if not r.is_empty_case]
    if not scored:
        return 0.0
    hits = sum(1 for r in scored if set(r.expected) & set(r.returned[:k]))
    return hits / len(scored)


def empty_case_accuracy(results: list[CaseResult]) -> float:
    empties = [r for r in results if r.is_empty_case]
    if not empties:
        return 1.0
    return sum(1 for r in empties if not r.returned) / len(empties)


@dataclass
class FrontierPoint:
    threshold: float
    recall_at_3: float
    empty_accuracy: float


def sweep_thresholds(index, cases, scorer, thresholds, *, limit: int = 5) -> list[FrontierPoint]:
    """同一个打分器、同一份语料，只挪阈值。

    复用同一个 scorer 实例是**必须的**：向量打分器的缓存让整轮扫描只编码
    一次语料。每个阈值新建一个 scorer 会把扫描变成 N 倍的模型调用。

    注意：`LiteralScorer.min_relevance` 是类属性，`original = scorer.min_relevance`
    读到的是类属性；扫完在 `finally` 里写回时会在这个实例上留下一个同名的
    **实例属性**（遮蔽类属性，值相同，功能无害）。以后若有代码假设
    "读类属性就等于读某个 scorer 实例的默认阈值"，这里会不成立。
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


def threshold_range(start: float, stop: float, step: float) -> list[float]:
    """生成 `[start, stop]` 闭区间内、步长 `step` 的阈值列表（含两端）。

    点数按 `round((stop - start) / step)` 计算，而不是 `int(...)`：
    浮点误差会让 `int()` 把本该是整数的点数悄悄截掉一个 —— 默认参数
    `0.05 / 0.95 / 0.05` 下 `(0.95-0.05)/0.05` 在浮点里是
    `17.999999999999996`，`int()` 截成 17，用户显式要求的终点 0.95
    就被静默丢了。`round()` 对这种"差一点点到整数"的浮点误差是安全的。
    """
    steps = round((stop - start) / step)
    return [round(start + i * step, 4) for i in range(steps + 1)]


def verdict(results: list[CaseResult]) -> int:
    """只有 recall@3 掉线才红。

    空样本失手（该空却凑出候选）记录但不红：那种候选到了消费端 LLM 手里
    还有机会被丢掉，而闸门若为它变红，唯一的修法是调高 MIN_COVERAGE，
    代价是掉真实召回 —— 两边代价不对称，闸门就该按不对称来定
    （同 eval_fidelity.py 的『误杀非零即红、漏报记基线』）。
    """
    return 0 if recall_at(results, 3) >= RECALL_AT_3_THRESHOLD else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="评测诊断检索的召回")
    parser.add_argument("--graph", type=Path, default=DEFAULT_GRAPH)
    parser.add_argument("--groundtruth", type=Path, default=GROUNDTRUTH)
    parser.add_argument("--limit", type=int, default=5, help="每条观察句取回多少候选")
    parser.add_argument("--scorer", choices=("literal", "vector"), default="literal")
    parser.add_argument("--model", default=None, help=f"向量打分器用哪个模型（默认 {DEFAULT_MODEL}）")
    parser.add_argument("--threshold-sweep", action="store_true", help="扫描阈值并打印前沿曲线")
    parser.add_argument("--sweep-from", type=float, default=0.05, help="扫描起点")
    parser.add_argument("--sweep-to", type=float, default=0.95, help="扫描终点")
    parser.add_argument("--sweep-step", type=float, default=0.05, help="扫描步长")
    args = parser.parse_args()

    if args.scorer == "vector":
        from cn_curriculum_graph.serve.embedding import build_embedder

        scorer = VectorScorer(build_embedder(args.model))
    else:
        scorer = LiteralScorer()

    started = time.perf_counter()
    index = GraphIndex(load_graph(args.graph), scorer=scorer)
    warm_seconds = time.perf_counter() - started

    cases = load_cases(args.groundtruth)

    if args.threshold_sweep:
        thresholds = threshold_range(args.sweep_from, args.sweep_to, args.sweep_step)
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

        if args.scorer == "vector":
            # 字面基线现算，不许写死：硬编码的数字会在阈值扫描改动、
            # groundtruth 增删样本后悄悄过期，且早就被本脚本自己的
            # --threshold-sweep 实测推翻过一次（见 CHANGELOG/commit）。
            # 这一趟只用 LiteralScorer，不碰任何可选依赖，几秒内跑完。
            baseline_index = GraphIndex(index.graph, scorer=LiteralScorer())
            baseline_frontier = sweep_thresholds(
                baseline_index, cases, baseline_index.scorer, thresholds, limit=args.limit
            )
            baseline_best = same_operating_point(baseline_frontier)
            if baseline_best is None:
                print("  （字面基线在扫描范围内未达到该工作点，无法对比）")
            else:
                print(
                    f"  （字面基线在同一工作点上：阈值 {baseline_best.threshold} "
                    f"→ recall@3 = {baseline_best.recall_at_3:.0%}）"
                )

        print(f"\n代价：建索引 {warm_seconds:.1f}s，编码 {getattr(scorer, 'encode_calls', 0)} 条文本")
        return 0

    results = run_cases(index, cases, limit=args.limit)

    print(f"图：{args.graph}    样本：{len(cases)}    候选上限：{args.limit}\n")
    print(f"{'@1':<4}{'@3':<4}{'@5':<4}{'样本 id':<34}命中情况")
    print("-" * 100)
    for r in results:
        marks = []
        for k in (1, 3, 5):
            if r.is_empty_case:
                marks.append("—")
            else:
                marks.append("✓" if set(r.expected) & set(r.returned[:k]) else "✗")
        if r.is_empty_case:
            detail = "（应当为空）" + ("✓ 空" if not r.returned else f"✗ 凑出 {r.hit_names[:2]}")
        else:
            detail = "，".join(r.hit_names[:3]) or "空手而归"
        print(f"{marks[0]:<4}{marks[1]:<4}{marks[2]:<4}{r.case_id:<34}{detail}")

    scored = [r for r in results if not r.is_empty_case]
    print("-" * 100)
    for k in (1, 3, 5):
        print(f"recall@{k} = {recall_at(results, k):.0%}  ({int(recall_at(results, k) * len(scored))}/{len(scored)})")

    empties = [r for r in results if r.is_empty_case]
    print(f"空样本正确率 = {empty_case_accuracy(results):.0%}  ({len(empties)} 条应当召回不到)")

    print("\n未命中的样本（逐条看比看平均值有用）：")
    for r in scored:
        if not set(r.expected) & set(r.returned[:3]):
            print(f"  ✗ {r.case_id}：{r.observation}")
            print(f"     期望 {r.expected}｜实得 {r.returned[:3] or '空'}｜{r.probes}")

    code = verdict(results)
    if code:
        print(
            f"\n❌ recall@3 = {recall_at(results, 3):.0%} < 阈值 {RECALL_AT_3_THRESHOLD:.0%}。"
            "\n   下一步不是调 MIN_COVERAGE，是回到 docs/mcp-server-design.md §1"
            "重新论证纯检索够不够。"
        )
    else:
        print(f"\n✅ recall@3 = {recall_at(results, 3):.0%} ≥ 阈值 {RECALL_AT_3_THRESHOLD:.0%}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
