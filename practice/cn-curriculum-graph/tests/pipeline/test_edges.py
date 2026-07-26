"""连边层。

剪枝规则**直接由校验规则反推**：GRADE_INVERSION 会拒绝"前置年级晚于后继"，
那就干脆不生成这类候选。生成端和校验端共用同一套约束，
省的不只是调用次数 —— 同一套约束写两遍才是 bug 的温床。
"""

from types import SimpleNamespace

from cn_curriculum_graph.pipeline.edges import (
    EDGE_TOOL_NAME,
    MAX_GRADE_GAP,
    DeepSeekEdgeProposer,
    candidate_prerequisites,
    propose_all,
)
from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, ProposedEdgeBatch, TopicDraft


def _draft(draft_id: str, grade: int, name: str = "某知识点") -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id="c#001",
        standard_codes=["3.1.1"],
        content=DraftContent(
            name=name,
            description="描述",
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=grade,
            grade_end=grade,
            evidence=["证据"],
            assessment_prompt="问一句",
            source_span="原文",
        ),
    )


def test_max_grade_gap_is_two():
    assert MAX_GRADE_GAP == 2


def test_candidates_exclude_later_grades():
    early, late = _draft("a", grade=3), _draft("b", grade=5)

    candidates = candidate_prerequisites([early, late])

    assert [c.draft_id for c in candidates["b"]] == ["a"]
    assert candidates["a"] == []


def test_candidates_exclude_gaps_beyond_the_limit():
    early, far = _draft("a", grade=1), _draft("b", grade=5)

    assert candidate_prerequisites([early, far])["b"] == []


def test_same_grade_drafts_are_mutual_candidates():
    left, right = _draft("a", grade=4), _draft("b", grade=4)

    candidates = candidate_prerequisites([left, right])

    assert [c.draft_id for c in candidates["a"]] == ["b"]
    assert [c.draft_id for c in candidates["b"]] == ["a"]


def test_a_draft_is_never_its_own_candidate():
    only = _draft("a", grade=4)

    assert candidate_prerequisites([only])["a"] == []


def test_proposes_edges_per_draft_and_keeps_only_known_prerequisites():
    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="先学 a")]
        )

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4)], proposer)

    assert [e.prerequisite_draft_id for e in edges["b"]] == ["a"]
    assert drops == []


def test_edges_pointing_at_unknown_drafts_are_dropped():
    """模型可能编一个不存在的 id 出来 —— 丢掉并记账，别让它污染图。"""

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[ProposedEdge(prerequisite_draft_id="不存在", strength="hard", reason="乱说")]
        )

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4)], proposer)

    assert edges["b"] == []
    assert drops[0].reason == "UNKNOWN_PREREQUISITE"
    assert "不存在" in drops[0].detail


def test_drafts_without_candidates_skip_the_model_entirely():
    calls = []

    def proposer(target, candidates):
        calls.append(target.draft_id)
        return ProposedEdgeBatch(edges=[])

    propose_all([_draft("a", 3)], proposer)

    # 没有候选就没有可问的，省一次调用
    assert calls == []


def test_a_failing_draft_is_dropped_without_stopping_the_batch():
    def proposer(target, candidates):
        raise RuntimeError("API 炸了")

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4)], proposer)

    assert edges["b"] == []
    assert drops[0].reason == "EDGES_FAILED"
    assert "API 炸了" in drops[0].detail


def test_self_referencing_edge_is_dropped():
    """target 自己是 known 的（它当然存在），但绝不该是自己的候选前置。

    候选池天然排除自身，可若判定器（或未来别的实现）返回了自环，
    仅凭"id 是否存在于全体 draft 里"这条校验是拦不住的 —— 必须对照
    「实际发给它的那份候选池」，而不是对照全体草稿的并集。
    """

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[ProposedEdge(prerequisite_draft_id=target.draft_id, strength="hard", reason="我是我自己的前置")]
        )

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4)], proposer)

    assert edges["b"] == []
    assert drops[0].reason == "NON_CANDIDATE_PREREQUISITE"
    assert "b" in drops[0].detail


def test_edge_pointing_outside_the_candidate_pool_is_dropped():
    """c 是真实存在的草稿，但对 b 来说年级更晚，从未进入 b 的候选池。

    模型若"越权"引用它，等价于产出一条 GRADE_INVERSION 的边 ——
    剪枝的全部意义就是不让这种边有机会被生成，因此校验也必须
    对照候选池，而不是只看"这个 id 是不是某个真实草稿"。
    """

    def proposer(target, candidates):
        if target.draft_id != "b":
            return ProposedEdgeBatch(edges=[])
        return ProposedEdgeBatch(
            edges=[ProposedEdge(prerequisite_draft_id="c", strength="hard", reason="越权引用")]
        )

    edges, drops = propose_all([_draft("a", 3), _draft("b", 4), _draft("c", 8)], proposer)

    assert edges["b"] == []
    assert drops[0].reason == "NON_CANDIDATE_PREREQUISITE"
    assert "c" in drops[0].detail


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[SimpleNamespace(type="tool_use", name=EDGE_TOOL_NAME, input=tool_input)]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_edge_proposer_forces_its_tool_and_shows_candidate_ids():
    recorder: dict = {}
    client = _fake_client(
        recorder,
        {"edges": [{"prerequisite_draft_id": "a", "strength": "hard", "reason": "先学 a"}]},
    )

    batch = DeepSeekEdgeProposer(client=client)(_draft("b", 4), [_draft("a", 3, name="甲")])

    assert [e.prerequisite_draft_id for e in batch.edges] == ["a"]
    assert recorder["tool_choice"] == {"type": "tool", "name": EDGE_TOOL_NAME}
    assert recorder["thinking"] == {"type": "disabled"}
    assert recorder["temperature"] == 0
    # 候选的 id 必须出现在提示里，否则模型没法引用它们
    assert "a" in str(recorder["messages"])
