"""命令行入口：跑一次校验，按结果决定退出码。

    uv run ccg-validate data/graph.json

有 ERROR 退出码 1（CI 红），只有 WARNING 退出码 0（CI 绿但留痕）。
"""

import argparse
import collections
import os
from pathlib import Path

from cn_curriculum_graph.judges.anthropic_judge import DEFAULT_MODEL as ANTHROPIC_MODEL
from cn_curriculum_graph.judges.anthropic_judge import AnthropicJudge
from cn_curriculum_graph.judges.deepseek_judge import DEFAULT_MODEL as DEEPSEEK_MODEL
from cn_curriculum_graph.judges.deepseek_judge import DeepSeekJudge
from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.runner import has_errors, run_all
from cn_curriculum_graph.validators.base import Finding, Severity
from cn_curriculum_graph.validators.consistency import Judge

JUDGE_KINDS = ("none", "anthropic", "deepseek")

# 每个 judge 认自己的 key：DeepSeek 绝不复用 ANTHROPIC_API_KEY，
# 也不走官方文档教的 ANTHROPIC_BASE_URL —— 那会把同机的 Claude Code 一起劫持。
_REQUIRED_ENV = {"anthropic": "ANTHROPIC_API_KEY", "deepseek": "DEEPSEEK_API_KEY"}
_DEFAULT_MODEL = {"anthropic": ANTHROPIC_MODEL, "deepseek": DEEPSEEK_MODEL}


def default_model_for(kind: str) -> str:
    """--model 没给时按 judge 取默认，别把 claude 的 id 发给 DeepSeek。"""
    return _DEFAULT_MODEL[kind]


def build_judge(kind: str, model: str) -> Judge | None:
    """按 --judge 选择判定器。none → 返回 None（runner 会留下 CONSISTENCY_SKIPPED
    警告，不静默通过）；其余 → 真 LLM judge，激活 NAME_DESC_MISMATCH。"""
    if kind == "none":
        return None
    if kind == "anthropic":
        return AnthropicJudge(model=model)
    if kind == "deepseek":
        return DeepSeekJudge(model=model)
    raise ValueError(f"未知 judge：{kind}")


def load_graph(path: str | Path) -> CurriculumGraph:
    """严格解析：schema 里 extra='forbid'，未知字段直接报错而不是静默忽略。"""
    return CurriculumGraph.model_validate_json(Path(path).read_text(encoding="utf-8"))


def format_report(findings: list[Finding]) -> str:
    if not findings:
        return "✅ 校验通过，未发现问题"

    errors = [f for f in findings if f.severity is Severity.ERROR]
    warnings = [f for f in findings if f.severity is Severity.WARNING]

    lines = [f"{len(errors)} error, {len(warnings)} warning", ""]

    # 按 (code, severity) 分组：同一个 code 的严重级可能不同
    # （如 GRADE_INVERSION 在 hard 边是 ERROR、soft 边是 WARNING），
    # 只按 code 分组会让整组跟着第一条走，把 error 藏进 warning 里。
    by_group: dict[tuple[str, Severity], list[Finding]] = collections.defaultdict(list)
    for f in findings:
        by_group[(f.code, f.severity)].append(f)

    # 错误在前，同级按数量降序——先看最成片的问题
    for (code, severity), group in sorted(
        by_group.items(),
        key=lambda kv: (kv[0][1] is not Severity.ERROR, -len(kv[1])),
    ):
        mark = "✗" if severity is Severity.ERROR else "!"
        lines.append(f"{mark} {code} × {len(group)}")
        for f in group[:5]:
            lines.append(f"    {f.message}")
        if len(group) > 5:
            lines.append(f"    …… 另有 {len(group) - 5} 条同类问题")
        lines.append("")

    return "\n".join(lines).rstrip()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="ccg-validate", description="课标知识依赖图校验")
    parser.add_argument("graph", help="图 JSON 文件路径")
    parser.add_argument(
        "--judge",
        choices=JUDGE_KINDS,
        default="none",
        help="name/description 语义一致性判定器（各自需要对应的 API key）",
    )
    parser.add_argument(
        "--model",
        default=None,
        help=f"judge 模型（不给则按 judge 取默认：anthropic={ANTHROPIC_MODEL}，deepseek={DEEPSEEK_MODEL}）",
    )
    args = parser.parse_args(argv)

    env_var = _REQUIRED_ENV.get(args.judge)
    if env_var and not os.environ.get(env_var):
        parser.error(f"--judge {args.judge} 需要 {env_var}（export 或写进 .env）")

    model = args.model or (default_model_for(args.judge) if args.judge != "none" else "")
    judge = build_judge(args.judge, model=model)
    findings = run_all(load_graph(args.graph), judge=judge)
    print(format_report(findings))
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
