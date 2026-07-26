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

    # 正类 = 名实不符（expected_consistent=false），这是我们真正想抓住的
    tp = fp = fn = tn = 0
    print(f"模型：{model}    样本：{len(cases)}\n")
    print(f"{'结果':<4}{'id':<18}{'期望':<8}{'判定':<8}理由")
    print("-" * 80)
    for c in cases:
        verdict = judge(name=c["name"], description=c["description"])
        expected = c["expected_consistent"]
        got = verdict.consistent
        ok = expected == got
        # 混淆矩阵（以『不符』为正类）
        if not expected and not got:
            tp += 1
        elif expected and not got:
            fp += 1
        elif not expected and got:
            fn += 1
        else:
            tn += 1
        mark = "✓" if ok else "✗ 错"
        exp_s = "一致" if expected else "不符"
        got_s = "一致" if got else "不符"
        print(f"{mark:<4}{c['id']:<18}{exp_s:<8}{got_s:<8}{verdict.reason[:40]}")

    total = len(cases)
    acc = (tp + tn) / total if total else 0.0
    prec = tp / (tp + fp) if (tp + fp) else float("nan")
    rec = tp / (tp + fn) if (tp + fn) else float("nan")
    print("-" * 80)
    print(f"准确率 accuracy = {acc:.0%}  ({tp + tn}/{total})")
    print(f"查准 precision = {prec:.0%}   查全 recall = {rec:.0%}   (正类=名实不符)")
    print(f"混淆矩阵：抓到不符 TP={tp}  漏报 FN={fn}  误报 FP={fp}  正确放过 TN={tn}")

    # 漏掉一个已知的名实不符（FN），说明这个模型不够格，非零退出好让 CI/脚本感知
    return 1 if fn else 0


if __name__ == "__main__":
    raise SystemExit(main())
