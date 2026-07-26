"""用真 LLM judge 跑 ground truth，量它判名实一致准不准。

这一步的意义：CI 里的 NAME_DESC_MISMATCH 只有在 judge 靠谱时才有价值。
先拿手工核对出的已知案例当标尺，测准确率/查准/查全，再决定要不要升级模型。
这套 ground truth 基建将来直接被生成流水线的『交叉审核』层复用。

跑法（需要对应 provider 的 key）：
    uv run python scripts/eval_judge.py --judge deepseek          # 需 DEEPSEEK_API_KEY
    uv run python scripts/eval_judge.py --judge anthropic         # 需 ANTHROPIC_API_KEY
    uv run python scripts/eval_judge.py --judge deepseek --model deepseek-v4-pro  # 升级对比
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from cn_curriculum_graph.cli import _REQUIRED_ENV, build_judge, default_model_for

GROUNDTRUTH = Path(__file__).resolve().parent.parent / "data" / "judge-eval-groundtruth.json"


def main() -> int:
    parser = argparse.ArgumentParser(description="评测 name/description 一致性 judge")
    parser.add_argument("--judge", choices=("anthropic", "deepseek"), default="deepseek")
    parser.add_argument("--model", default=None, help="模型 id（不给则按 --judge 取默认）")
    parser.add_argument("--groundtruth", type=Path, default=GROUNDTRUTH)
    args = parser.parse_args()

    env_var = _REQUIRED_ENV[args.judge]
    if not os.environ.get(env_var):
        print(f"✗ --judge {args.judge} 需要 {env_var}（export 或写进 .env）。", file=sys.stderr)
        return 2

    model = args.model or default_model_for(args.judge)
    cases = json.loads(args.groundtruth.read_text(encoding="utf-8"))["cases"]
    judge = build_judge(args.judge, model=model)

    labels = ("consistent", "scope_mismatch", "topic_mismatch")
    short = {"consistent": "一致", "scope_mismatch": "范围不符", "topic_mismatch": "知识点错配"}
    confusion = {(e, g): 0 for e in labels for g in labels}

    print(f"模型：{model}    样本：{len(cases)}\n")
    print(f"{'结果':<4}{'id':<18}{'期望':<12}{'判定':<12}理由")
    print("-" * 92)
    for c in cases:
        verdict = judge(name=c["name"], description=c["description"])
        expected, got = c["expected_judgment"], verdict.judgment
        confusion[(expected, got)] += 1
        mark = "✓" if expected == got else "✗ 错"
        print(f"{mark:<4}{c['id']:<18}{short[expected]:<12}{short[got]:<12}{verdict.reason[:34]}")

    total = len(cases)
    correct = sum(confusion[(x, x)] for x in labels)
    print("-" * 92)
    print(f"准确率 accuracy = {correct / total:.0%}  ({correct}/{total})\n")

    # 每档单独看查准/查全：整体准确率会被样本最多的那档带着走
    print(f"{'判定档':<12}{'查准':<8}{'查全':<8}支持数")
    for x in labels:
        hit = confusion[(x, x)]
        predicted = sum(confusion[(e, x)] for e in labels)
        actual = sum(confusion[(x, g)] for g in labels)
        p = f"{hit / predicted:.0%}" if predicted else "—"
        r = f"{hit / actual:.0%}" if actual else "—"
        print(f"{short[x]:<12}{p:<8}{r:<8}{actual}")

    print("\n混淆矩阵（行=期望，列=判定）")
    print(f"{'':<12}" + "".join(f"{short[g]:<12}" for g in labels))
    for e in labels:
        print(f"{short[e]:<12}" + "".join(f"{confusion[(e, g)]:<12}" for g in labels))

    # CI 视角只有两件事致命：真错配被放过（漏报），或被降级成 WARNING（不再让 CI 红）。
    # scope↔consistent 之间的出入只影响噪声量，不影响"错的数据能不能被拦住"。
    missed = confusion[("topic_mismatch", "consistent")]
    downgraded = confusion[("topic_mismatch", "scope_mismatch")]
    if missed or downgraded:
        print(f"\n✗ 知识点错配被放过 {missed} 条、被降级成 warning {downgraded} 条 —— 这个模型不够格")
    return 1 if (missed or downgraded) else 0


if __name__ == "__main__":
    raise SystemExit(main())
