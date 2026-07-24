import json

from cn_curriculum_graph.cli import format_report, load_graph, main
from cn_curriculum_graph.models import Provenance, Standard
from cn_curriculum_graph.validators.base import Finding, Severity
from conftest import dep, graph, topic


def write_graph(tmp_path, g):
    path = tmp_path / "graph.json"
    path.write_text(g.model_dump_json(), encoding="utf-8")
    return path


def clean(tid, **kw):
    return topic(
        tid,
        standards=[Standard(curriculum="cn-moe-math-2022", code="3.1.2")],
        provenance=Provenance(method="hand-authored"),
        **kw,
    )


def test_load_graph_reads_json_file(tmp_path):
    g = graph(topics=[clean("A", grade_start=4), clean("B", grade_start=3)],
              dependencies=[dep("A", "B")])
    path = write_graph(tmp_path, g)

    loaded = load_graph(path)

    assert [t.id for t in loaded.topics] == ["A", "B"]
    assert loaded.dependencies[0].prerequisite_id == "B"


def test_load_graph_rejects_unknown_field(tmp_path):
    path = tmp_path / "bad.json"
    path.write_text(json.dumps({"topics": [], "surprise": 1}), encoding="utf-8")

    try:
        load_graph(path)
    except Exception as exc:
        assert "surprise" in str(exc)
    else:
        raise AssertionError("schema 应当拒绝未知字段")


def test_report_shows_counts_per_severity():
    findings = [
        Finding(code="CYCLE", severity=Severity.ERROR, message="有环"),
        Finding(code="ISOLATED_TOPIC", severity=Severity.WARNING, message="孤立"),
    ]

    report = format_report(findings)

    assert "1 error" in report
    assert "1 warning" in report
    assert "CYCLE" in report


def test_report_on_clean_graph_says_so():
    assert "通过" in format_report([])


def test_same_code_with_mixed_severity_is_split_not_labelled_by_the_first_one():
    # GRADE_INVERSION 在 hard 边是 ERROR、soft 边是 WARNING。
    # 按 code 单独分组会让整组跟着第一条的严重级走，把 error 显示成 warning。
    findings = [
        Finding(code="GRADE_INVERSION", severity=Severity.WARNING, message="soft 倒挂"),
        Finding(code="GRADE_INVERSION", severity=Severity.ERROR, message="hard 倒挂"),
    ]

    report = format_report(findings)

    assert "✗ GRADE_INVERSION × 1" in report
    assert "! GRADE_INVERSION × 1" in report


def test_main_exits_nonzero_when_errors_present(tmp_path):
    # 有环 → ERROR → CI 必须失败
    g = graph(topics=[clean("A"), clean("B")], dependencies=[dep("A", "B"), dep("B", "A")])
    path = write_graph(tmp_path, g)

    assert main([str(path)]) == 1


def test_main_exits_zero_when_only_warnings(tmp_path):
    # 不传 judge 只会产生 CONSISTENCY_SKIPPED 警告 → 放行
    g = graph(topics=[clean("A", grade_start=4), clean("B", grade_start=3)],
              dependencies=[dep("A", "B")])
    path = write_graph(tmp_path, g)

    assert main([str(path)]) == 0
