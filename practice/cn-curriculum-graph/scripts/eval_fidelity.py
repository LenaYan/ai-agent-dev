"""量 fidelity 三档判定器准不准 —— 3×3 混淆矩阵，漏报/降级非零退出。

与 `eval_judge.py` 同一套约定（那是 name/desc 三档判定的评测脚本）：
**有 ground truth 才敢信 LLM 判定**。这里把同一条原则套到 fidelity 上。

## 退出码语义（与 eval_judge.py 一致的分级）

不是"准确率低就红"，而是按**错的方向**分级 —— 两个方向的代价完全不对称：

- **漏报（fabricated 被判成 faithful / reasonable_elaboration）**：真编造混进图里，
  下游 agent 会拿着编造的知识点去教孩子。**这是最贵的错，必须红。**
- **误杀（reasonable_elaboration 被判成 fabricated）**：合格节点被淘汰。
  这正是 2026-07-27 那次全量运行的病灶 —— 62% 淘汰率、图稀疏到没法做路径规划。
  **也必须红**，否则改了三档等于没改。
- **档内混淆（faithful ↔ reasonable_elaboration）**：两档后果相同（都保留），
  不影响产出，只影响留痕精度。**记录但不红。**

跑法：

    DEEPSEEK_API_KEY=... uv run python scripts/eval_fidelity.py
    DEEPSEEK_API_KEY=... uv run python scripts/eval_fidelity.py --model deepseek-v4-pro
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from cn_curriculum_graph.pipeline.models import DraftContent, TopicDraft  # noqa: E402
from cn_curriculum_graph.pipeline.review import (  # noqa: E402
    DEFAULT_MODEL,
    DeepSeekFidelityJudge,
    FidelityJudgment,
)

GROUND_TRUTH = Path(__file__).resolve().parents[1] / "data" / "fidelity-eval-groundtruth.json"
TIERS: tuple[str, ...] = ("faithful", "reasonable_elaboration", "fabricated")

# **为什么基线不是 0**（2026-07-27，deepseek-v4-flash 实测 17/20，误杀 0、漏报 2）
#
# 两条漏报的性质不同，都不是"再调调 prompt 就能压掉"：
#
# 1. 「解决简单的实际问题」→「运用**计算器**处理…」：**根因不在判定器**。
#    课标 2.2.2 原文是「能借助计算器进行计算，解决简单的实际问题」—— 计算器
#    本来就在原文里，是抽取层截 `source_span` 时截窄了，判定器拿到的是残缺
#    上下文。该修的是抽取层，不是这里。
# 2. 「万以内数的意义」→「包括…大小比较…」：真边界。「数的意义」是否涵盖
#    「大小比较」，两种读法都成立。
#
# 试过加一条针对性规则压它们，**实测更差**（漏报 2→3，见 review.py 里的失败
# 记录）—— 那条规则把注意力全引向「多写」，挤掉了「少写（内容缺失）」。
#
# 于是把闸门按**代价不对称**重新定：
# - **误杀非零即红**：误杀让图稀疏到没法用，这是已实测的病灶（62% 淘汰率、
#   347 条边只活 6 条）。
# - **漏报记基线、变差才红**：漏报让个别编造节点混进一张
#   `review_status=unreviewed` / `confidence=0.0` 的图 —— 那正是 provenance
#   诚实性设计要兜的场景。
#
# 基线是**已知限制的显式记录，不是目标**。把它调低要靠证据，不是靠调低期望。
LEAKED_BASELINE = 2

# 两档都是"保留"，混淆它们不影响产出 —— 只影响留痕精度。
_KEEP = {"faithful", "reasonable_elaboration"}


def _as_draft(source_span: str, description: str) -> TopicDraft:
    """判定器的输入是 TopicDraft，但它只读 description 与 source_span 两个字段。

    其余字段填能过 schema 校验的最小合法值 —— 让它们出现在评测里没有意义，
    反而会让人误以为判定结果与年级/领域有关。
    """
    return TopicDraft(
        draft_id="eval",
        chunk_id="eval",
        standard_codes=[],
        content=DraftContent(
            name="评测样本",
            description=description,
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=1,
            grade_end=1,
            evidence=["评测占位"],
            assessment_prompt="评测占位",
            source_span=source_span,
        ),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="eval-fidelity", description="fidelity 三档判定评测")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    args = parser.parse_args(argv)

    if not os.environ.get("DEEPSEEK_API_KEY"):
        parser.error("需要 DEEPSEEK_API_KEY（export 或写进 .env）")

    cases = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["cases"]
    judge = DeepSeekFidelityJudge(model=args.model)

    matrix: Counter[tuple[str, str]] = Counter()
    misses: list[tuple[dict, str]] = []

    print(f"模型={args.model}  样本={len(cases)}\n")
    for case in cases:
        verdict = judge(_as_draft(case["source_span"], case["description"]))
        expected, got = case["expected"], verdict.judgment
        matrix[(expected, got)] += 1
        if expected != got:
            misses.append((case, got))
        mark = "✓" if expected == got else "✗"
        print(f"{mark} 期望 {expected:22s} 实得 {got:22s} 「{case['source_span'][:22]}」")

    print("\n混淆矩阵（行=期望，列=实得）")
    header = " " * 24 + "".join(f"{t:>24s}" for t in TIERS)
    print(header)
    for exp in TIERS:
        row = "".join(f"{matrix[(exp, got)]:>24d}" for got in TIERS)
        print(f"{exp:24s}{row}")

    correct = sum(matrix[(t, t)] for t in TIERS)
    print(f"\n准确率 {correct}/{len(cases)} = {correct / len(cases):.0%}")

    # 按方向分级 —— 不看总准确率看错的方向
    leaked = sum(matrix[("fabricated", got)] for got in _KEEP)
    killed = sum(matrix[(exp, "fabricated")] for exp in _KEEP)
    tier_confusion = matrix[("faithful", "reasonable_elaboration")] + matrix[
        ("reasonable_elaboration", "faithful")
    ]
    print(f"漏报（编造被放行）    {leaked}   ← 非零即失败")
    print(f"误杀（合格被淘汰）    {killed}   ← 非零即失败")
    print(f"档内混淆（不影响产出）{tier_confusion}   ← 仅记录")

    if misses:
        print("\n判错的样本：")
        for case, got in misses:
            print(f"  期望 {case['expected']} 实得 {got}：「{case['source_span']}」")
            print(f"    {case.get('note', '')}")

    if killed:
        print("\n❌ 存在误杀 —— 合格节点被淘汰正是 2026-07-27 那次 62% 淘汰率的病灶。")
        return 1
    if leaked > LEAKED_BASELINE:
        print(f"\n❌ 漏报 {leaked} 超过已知基线 {LEAKED_BASELINE}，判定器退步了。")
        return 1
    if leaked:
        print(f"\n⚠️  漏报 {leaked}（= 已知基线，非退步）。见脚本文档的『为什么基线不是 0』。")
    else:
        print("\n✅ 无漏报无误杀。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
