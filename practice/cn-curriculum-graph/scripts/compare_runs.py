"""比两轮生成的图：身份稳不稳、**误概念还在不在**。

两个问题一次问完，因为它们都只在"跑完一轮新图"之后才有答案，而跑一轮
是 ~1000 次调用 / 11 分钟。分成两个脚本会诱使人只跑一个。

## 它回答 learning-log 上挂着的两件事

1. **注册表的红利到底兑现了没有**（2026-07-29 那轮是空表起步，认领不到
   任何旧身份，所以那一轮的 id 稳定性无从谈起）。要看的是 `id Jaccard`
   与 `id 留住但改名` 的条数 —— 后者才是注册表真正做功的证据：
   名字变了、身份没变，正是它被造出来要干的事。

2. **误概念的内容抖动**（至今零数据）。注册表只稳身份不稳内容，
   「加法交换律」节点还在、「对减法也成立」那条误概念没了 —— 这种
   丢失在身份指标上完全看不见。

跑法（不需要任何 key，纯字面运算）：

    uv run python scripts/compare_runs.py 上一轮/graph.json 这一轮/graph.json
    uv run python scripts/compare_runs.py A.json B.json --match-threshold 0.7
    uv run python scripts/compare_runs.py A.json B.json --limit 40

**先读明细再读百分比。** 这个项目已经三次靠"逐条打出来看"拦下过一个
好看但错误的结论（种子化注册表的假合并、边审的逐票统计、跨图比 recall）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cn_curriculum_graph.pipeline.drift import (  # noqa: E402
    DEFAULT_MATCH_THRESHOLD,
    DriftReport,
    compare_graphs,
    retention_curve,
)
from cn_curriculum_graph.serve.query import load_graph  # noqa: E402

CURVE_THRESHOLDS = (0.3, 0.4, 0.5, 0.6, 0.7, 0.8, 0.9)


def print_identity(report: DriftReport) -> None:
    print("== 身份层 ==")
    print(f"节点数：{len(report.ids_before)} → {len(report.ids_after)}")
    print(f"id Jaccard   = {report.id_jaccard:.0%}   （id 两轮都在：{len(report.survived_ids)}）")
    print(f"名称 Jaccard = {report.name_jaccard:.0%}")
    renamed = report.renamed_nodes
    print(f"id 留住但改名 = {len(renamed)} 个  ← 注册表做功的直接证据")
    for n in renamed:
        print(f"    {n.topic_id}  {n.name_before}  →  {n.name_after}")


def print_content(report: DriftReport, *, limit: int) -> None:
    print("\n== 内容层（只统计 id 两轮都在的节点，分母见 drift.py 模块文档）==")
    if not report.statements_before:
        print("上一轮这些节点里一条误概念都没有 —— 没有可丢的东西，这一层无话可说。")
        return
    print(f"上一轮误概念总数：{report.statements_before}（分布在 {len(report.nodes)} 个节点上）")
    print(f"原文未改留存   = {report.verbatim_retention:.0%}")
    print(
        f"含改写留存     = {report.matched_retention:.0%}"
        f"   （阈值 {report.threshold}，两者之差 = 被改写的量）"
    )
    print(f"配对相关度中位数 = {report.median_matched_score:.3f}   （只统计配对成功的，偏高是设计如此）")

    hollowed = report.hollowed_nodes
    print(f"\n空壳节点（身份留住、误概念全丢）= {len(hollowed)} 个")
    for n in hollowed[:limit]:
        print(f"    {n.topic_id}  {n.name_after}   丢了 {len(n.lost)} 条")

    lost = report.lost_statements
    print(f"\n丢失的误概念（{len(lost)} 条，逐条看比看平均值有用）：")
    for topic_id, name, statement in lost[:limit]:
        print(f"    [{name}] {statement}")
    if len(lost) > limit:
        print(f"    …… 还有 {len(lost) - limit} 条，--limit 调大")


def print_curve(before, after) -> None:
    print("\n== 配对阈值敏感性 ==")
    print("（DEFAULT_MATCH_THRESHOLD 没有实测标定，单点留存率只是那个阈值下的读数。")
    print("  曲线平 = 配对干脆、单点可信；一路滑坡 = 大量配对卡在门槛附近，那个数是抽签。）")
    print(f"\n{'阈值':<8}{'含改写留存':<14}空壳节点")
    print("-" * 34)
    for point in retention_curve(before, after, CURVE_THRESHOLDS):
        print(f"{point.threshold:<8}{point.matched_retention:<14.0%}{point.hollowed}")


def main() -> int:
    parser = argparse.ArgumentParser(description="比两轮生成的图：身份 + 误概念内容漂移")
    parser.add_argument("before", type=Path, help="基线（上一轮）graph.json")
    parser.add_argument("after", type=Path, help="新一轮 graph.json")
    parser.add_argument(
        "--match-threshold",
        type=float,
        default=DEFAULT_MATCH_THRESHOLD,
        help=f"两条 statement 算同一条的相关度门槛（默认 {DEFAULT_MATCH_THRESHOLD}，未标定）",
    )
    parser.add_argument("--limit", type=int, default=20, help="明细最多打印多少条")
    parser.add_argument("--no-curve", action="store_true", help="跳过阈值敏感性扫描")
    args = parser.parse_args()

    before = load_graph(args.before)
    after = load_graph(args.after)
    report = compare_graphs(before, after, threshold=args.match_threshold)

    print(f"基线：{args.before}\n新轮：{args.after}\n")
    print_identity(report)
    print_content(report, limit=args.limit)
    if not args.no_curve:
        print_curve(before, after)

    # 这个脚本**不设闸门、永远返回 0**：漂移多少算"太多"目前没有任何依据，
    # 拍一个数当阈值只会造出第四个"看起来有根据"的假指标。它是量具不是判据。
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
