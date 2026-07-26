"""切分是纯规则层。

编号必须在这一层绑定：校验层的 LOW_STANDARDS_COVERAGE 是带硬阈值的 ERROR，
抽不到编号足以让整批产出被自己的 CI 拒掉。而编号在原文里是有格式的，
纯规则就能拿，交给模型只会平白引入不确定性。
"""

from cn_curriculum_graph.pipeline.chunk import split_source

SOURCE = """3.1.2 能理解小数的意义，会比较小数的大小。

3.1.3 能进行简单的小数加减运算。
会解决相关的简单实际问题。

这一段没有条目编号，属于导言。

4.2.1 能认识常见的平面图形。
"""


def test_splits_paragraphs_into_numbered_chunks():
    chunks, _ = split_source(SOURCE, source_file="math.md")

    assert [c.standard_code for c in chunks] == ["3.1.2", "3.1.3", "4.2.1"]


def test_chunk_ids_are_deterministic_and_ordered():
    chunks, _ = split_source(SOURCE, source_file="math.md")

    assert [c.id for c in chunks] == ["math#001", "math#002", "math#003"]
    assert [c.ordinal for c in chunks] == [1, 2, 3]


def test_chunk_text_keeps_the_whole_paragraph_without_the_code():
    chunks, _ = split_source(SOURCE, source_file="math.md")

    second = chunks[1]
    assert second.text.startswith("能进行简单的小数加减运算")
    assert "会解决相关的简单实际问题" in second.text
    assert not second.text.startswith("3.1.3")


def test_paragraph_without_a_code_is_dropped_with_a_reason():
    _, drops = split_source(SOURCE, source_file="math.md")

    assert len(drops) == 1
    assert drops[0].stage == "chunk"
    assert drops[0].reason == "NO_STANDARD_CODE"
    assert "导言" in drops[0].detail


def test_single_level_number_is_not_a_standard_code():
    """『1. 前言』这种列表序号不是条目编号，要求至少两级。"""
    chunks, drops = split_source("1. 前言部分。", source_file="math.md")

    assert chunks == []
    assert drops[0].reason == "NO_STANDARD_CODE"


def test_empty_source_yields_nothing():
    assert split_source("", source_file="math.md") == ([], [])
