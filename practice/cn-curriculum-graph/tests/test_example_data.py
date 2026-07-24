"""随仓库发布的示例数据必须始终通过校验。

作用是回归防护：schema 或校验规则演进时，示例数据不能静默失效——
一份连自己示例都过不了的 schema，说明改动没想清楚。
"""

from pathlib import Path

from cn_curriculum_graph.cli import load_graph
from cn_curriculum_graph.runner import has_errors, run_all

EXAMPLE = Path(__file__).resolve().parent.parent / "data" / "example-graph.json"


def test_example_graph_passes_validation():
    findings = run_all(load_graph(EXAMPLE))

    assert not has_errors(findings), [f.message for f in findings]


def test_example_graph_exercises_the_four_differentiating_fields():
    # 示例数据要真的用上相对 Marble 的四处差异，否则它证明不了 schema 可用
    graph = load_graph(EXAMPLE)

    assert any(t.misconceptions for t in graph.topics), "缺 misconceptions 示例"
    assert graph.revisits, "缺 revisits 示例"
    assert all(t.provenance for t in graph.topics), "缺 provenance"
    assert any(t.textbook_units for t in graph.topics), "缺教材单元对齐示例"
