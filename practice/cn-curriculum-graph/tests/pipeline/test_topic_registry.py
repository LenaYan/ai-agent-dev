"""稳定 id 注册表：让节点身份活过一次重跑。

**为什么需要它**（docs/pipeline-reproducibility.md）：`make_topic_id` 原来是
`sha1(name|domain|grade_start)`，而实测下来同一份课标原文跑三次，节点名的
动荡里**约七成是同义改写**（"计数单位的感悟"→"计数单位"、"了解十进制计数法"
→"十进制计数法"）。名字删两个字 id 就变，24 个评测标签死了 12 个。

**为什么不用位置编号**：`chunk_id + 序号` 的标签存活率是 92%（比注册表的 83%
还高），但它带来 **5 个假合并** —— 不同的知识点悄悄共用同一个 id。id 变动至少
会被发现（标签失效、评测拒跑），假合并是静默的错误身份。代价不对称，所以取
注册表。四个候选方案的实测对比见该文档。
"""

import json
from pathlib import Path

from cn_curriculum_graph.pipeline.models import DraftContent
from cn_curriculum_graph.pipeline.topic_registry import (
    CLAIM_THRESHOLD,
    TopicRegistry,
    assign_topic_ids,
)


def _c(name: str, grade: int = 4) -> DraftContent:
    return DraftContent(
        name=name,
        description="随便",
        type="conceptual",
        subject="数学",
        domain="数与代数",
        grade_start=grade,
        grade_end=grade,
        evidence=["证据"],
        assessment_prompt="问一句",
        source_span="原文",
    )


def test_a_renamed_topic_claims_the_id_it_had_last_run():
    """核心用途：同义改写不该换身份。"""
    reg = TopicRegistry.empty()
    first = assign_topic_ids([_c("计数单位的感悟")], reg)

    second = assign_topic_ids([_c("计数单位")], reg)

    assert second[0] == first[0]


def test_two_similar_new_topics_cannot_both_claim_the_same_id():
    """一对一：否则两个不同的知识点会被合并成一个，那是静默的错误身份 ——
    正是不用「chunk_id + 序号」方案的原因。"""
    reg = TopicRegistry.empty()
    first = assign_topic_ids([_c("计数单位的感悟")], reg)
    old = first[0]

    second = assign_topic_ids([_c("计数单位"), _c("计数单位的意义")], reg)

    assert len(set(second)) == 2, "两个节点不能共用一个 id"
    assert old in second, "更像的那个应当认领到旧 id"


def test_an_unrelated_topic_gets_a_fresh_id():
    reg = TopicRegistry.empty()
    first = assign_topic_ids([_c("计数单位的感悟")], reg)

    second = assign_topic_ids([_c("质数与合数")], reg)

    assert second[0] != first[0]


def test_same_name_at_a_different_grade_is_a_different_topic():
    """同名但学段不同是两个知识点 —— 这条性质从旧方案继承，不能丢。"""
    reg = TopicRegistry.empty()
    a = assign_topic_ids([_c("分数的意义", grade=3)], reg)
    b = assign_topic_ids([_c("分数的意义", grade=5)], reg)

    assert a[0] != b[0]


def test_registry_keeps_the_original_name_as_anchor_and_records_aliases():
    """锚点不跟着漂：否则"计数单位的感悟→计数单位→单位"会一路飘走，
    几轮之后锚点和最初那个知识点已经对不上了。别名留痕，供人工复核。"""
    reg = TopicRegistry.empty()
    assign_topic_ids([_c("计数单位的感悟")], reg)
    assign_topic_ids([_c("计数单位")], reg)

    entry = reg.entries[0]
    assert entry.name == "计数单位的感悟", "锚点应保持首次登记的名称"
    assert "计数单位" in entry.aliases


def test_registry_roundtrips_through_json(tmp_path):
    reg = TopicRegistry.empty()
    assign_topic_ids([_c("计数单位的感悟")], reg)
    assign_topic_ids([_c("计数单位")], reg)
    path = tmp_path / "topic-registry.json"
    reg.save(path)

    again = TopicRegistry.load(path)
    ids = assign_topic_ids([_c("计数单位")], again)

    assert ids[0] == reg.entries[0].id
    assert json.loads(path.read_text(encoding="utf-8"))["threshold"] == CLAIM_THRESHOLD


def test_missing_registry_file_starts_empty_instead_of_crashing():
    """首次运行时注册表还不存在 —— 那是正常的起点，不是错误。"""
    reg = TopicRegistry.load(Path("/nonexistent/topic-registry.json"))

    assert reg.entries == []
