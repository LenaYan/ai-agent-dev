"""分层落盘：每层产物可单独重跑、可人眼检查。"""

from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline.models import Chunk, DropRecord, ProposedEdge, TargetedEdge


def _chunk(ordinal: int) -> Chunk:
    return Chunk(
        id=f"src#{ordinal:03d}",
        text="能理解小数的意义",
        standard_code="3.1.2",
        source_file="src.md",
        ordinal=ordinal,
    )


def test_round_trips_a_stage_file(tmp_path):
    path = tmp_path / "01-chunks.json"
    io.write_stage(path, [_chunk(1), _chunk(2)])

    loaded = io.read_stage(path, Chunk)

    assert [c.id for c in loaded] == ["src#001", "src#002"]


def test_stage_file_is_human_readable_utf8(tmp_path):
    path = tmp_path / "01-chunks.json"
    io.write_stage(path, [_chunk(1)])

    # 中文不能被转义成 \uXXXX —— 这些文件是给人看的
    assert "能理解小数的意义" in path.read_text(encoding="utf-8")


def test_append_drops_accumulates_across_stages(tmp_path):
    path = tmp_path / "dropped.json"
    io.append_drops(path, [DropRecord(stage="chunk", ref="a", reason="NO_STANDARD_CODE")])
    io.append_drops(path, [DropRecord(stage="review", ref="b", reason="VOTE_SPLIT")])

    loaded = io.read_stage(path, DropRecord)

    assert [d.stage for d in loaded] == ["chunk", "review"]


def test_read_stage_returns_empty_for_missing_file(tmp_path):
    assert io.read_stage(tmp_path / "nope.json", DropRecord) == []


def test_targeted_edge_round_trips_target_and_inner_edge(tmp_path):
    """补充：brief 未覆盖 TargetedEdge 的落盘。它是 ProposedEdge 的落盘包装 ——
    ProposedEdge 本身不记"这条边指向谁"，一旦写进文件再读回来，
    target_draft_id 和内层 edge 的字段都必须完整还原，否则边就废了。
    """
    path = tmp_path / "04-edges.json"
    targeted = TargetedEdge(
        target_draft_id="src#002-0",
        edge=ProposedEdge(
            prerequisite_draft_id="src#001-0",
            strength="hard",
            reason="必须先懂整数才能懂小数",
        ),
    )
    io.write_stage(path, [targeted])

    loaded = io.read_stage(path, TargetedEdge)

    assert len(loaded) == 1
    assert loaded[0].target_draft_id == "src#002-0"
    assert loaded[0].edge.prerequisite_draft_id == "src#001-0"
    assert loaded[0].edge.strength == "hard"
    assert loaded[0].edge.reason == "必须先懂整数才能懂小数"
