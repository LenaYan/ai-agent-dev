"""Marble 适配器的测试。

适配器只服务一个目的：把校验层跑在一份真实的、3000+ 条边的图上，
验证规则在规模下确实能抓到东西。**不是**为了引入 Marble 的内容。
"""

from cn_curriculum_graph.adapters.marble import from_marble

MARBLE_TOPIC = {
    "id": "mt_x",
    "type": "CONCEPTUAL",
    "subject": "Mathematics",
    "domain": "Fractions",
    "name": "Tenths",
    "description": "Count up and down in tenths",
    "ageRangeStart": 8,
    "ageRangeEnd": 9,
    "evidence": ["Count from 1/10 to 10/10"],
    "assessmentPrompt": "Can {{name}} count in tenths?",
    "standards": [],
}


def test_maps_age_to_chinese_grade():
    # 中国 6 岁上一年级，故 grade = age - 5
    g = from_marble({"topics": [MARBLE_TOPIC]}, {"dependencies": []})

    assert g.topics[0].grade_start == 3
    assert g.topics[0].grade_end == 4


def test_clamps_ages_outside_compulsory_education_range():
    young = {**MARBLE_TOPIC, "id": "y", "ageRangeStart": 4, "ageRangeEnd": 5}
    old = {**MARBLE_TOPIC, "id": "o", "ageRangeStart": 15, "ageRangeEnd": 15}

    g = from_marble({"topics": [young, old]}, {"dependencies": []})

    assert (g.topics[0].grade_start, g.topics[0].grade_end) == (1, 1)
    assert (g.topics[1].grade_start, g.topics[1].grade_end) == (9, 9)


def test_carries_edge_strength_and_reason():
    deps = {
        "dependencies": [
            {
                "topicId": "mt_x",
                "prerequisiteId": "mt_y",
                "strength": "hard",
                "reason": "需要先会数十分之几",
            }
        ]
    }

    g = from_marble({"topics": [MARBLE_TOPIC]}, deps)

    assert g.dependencies[0].strength == "hard"
    assert g.dependencies[0].reason == "需要先会数十分之几"


def test_carries_standards_so_coverage_stats_are_not_falsely_zero():
    # Marble 的 standards 是 "课标标识:条目编号" 的字符串
    aligned = {**MARBLE_TOPIC, "standards": ["ccss-math:4.NF.C.6", "uk-nc-2013:Y4.Fr.3"]}

    g = from_marble({"topics": [aligned]}, {"dependencies": []})

    assert [(s.curriculum, s.code) for s in g.topics[0].standards] == [
        ("ccss-math", "4.NF.C.6"),
        ("uk-nc-2013", "Y4.Fr.3"),
    ]


def test_supplies_placeholder_evidence_when_source_has_none():
    # Marble 有 35 个节点没有 evidence，而本 schema 要求至少一条
    bare = {**MARBLE_TOPIC, "evidence": []}

    g = from_marble({"topics": [bare]}, {"dependencies": []})

    assert g.topics[0].evidence == ["（源数据缺失）"]
