"""命令行入口：跑一次校验，按结果决定退出码。

    uv run ccg-validate data/graph.json

有 ERROR 退出码 1（CI 红），只有 WARNING 退出码 0（CI 绿但留痕）。
"""

import argparse
import collections
from pathlib import Path

from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.runner import has_errors, run_all
from cn_curriculum_graph.validators.base import Finding, Severity


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
    args = parser.parse_args(argv)

    findings = run_all(load_graph(args.graph))
    print(format_report(findings))
    return 1 if has_errors(findings) else 0


if __name__ == "__main__":
    raise SystemExit(main())
