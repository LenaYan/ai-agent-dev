"""领域层：纯检索 + 纯图算法，零 key、零成本、全可测。

**为什么这一层不塞 LLM**（这是本模块最有争议的一条决定，论证见
`docs/mcp-server-design.md` §1）：MCP server 的消费方本身就是一个 LLM，
它拿到候选自己会判断哪个对得上。在 server 里再放一个模型，是把同一个推理
做两遍 —— 正是 effective-agents 心法①"不要什么都做成 Agent"要拦的东西。

代价是中文检索只能靠字面：`"小数比大小"` 能命中，`"小数点后位数多就大"`
可能命中不了。这条不靠感觉判断够不够用，靠 `scripts/eval_diagnosis.py`
的 `recall@3` 说话。
"""

from __future__ import annotations

import json
from pathlib import Path

from pydantic import BaseModel

from cn_curriculum_graph.models import (
    CurriculumGraph,
    Dependency,
    Provenance,
    Revisit,
    Topic,
)
from cn_curriculum_graph.serve.scoring import LiteralScorer, Scorer

# 摘要长度：卡片是给 LLM 挑选用的，不是给它读全文用的。
# 想要全文有 get_topic。
SUMMARY_CHARS = 60

# description 命中相对 name 命中的折价。
# 不是 0（description 里藏着大量同义表述，`test_search_matches_description_when_name_misses`
# 就是这种形状），也不能等价（否则"某节点的描述里提了一句别的知识点"会
# 排到那个知识点自己前面）。
DESCRIPTION_WEIGHT = 0.5

# 误概念的 probe / correction_hint 相对 statement 的折价，同上。
SECONDARY_WEIGHT = 0.5


class TopicNotFoundError(ValueError):
    """请求的 topic_id 不在图里。

    继承 `ValueError` 不是随手挑的：按 `docs/error-taxonomy.md`，
    `ValueError` 在本项目里专表"确定性错误"，`retry_on` 一律排除它。
    id 不存在再试一百次也还是不存在，重试纯属浪费。
    """


class TopicCard(BaseModel):
    """检索结果的精简卡片。

    带 `grade_start/end` 不是为了好看：生成的图里同名节点是高危区
    （Marble 全量数据上 86% 的名实不符 ERROR 落在跨年龄段复用的同名节点上），
    只回名字的话 agent 无从分辨命中的是三年级那个还是五年级那个。
    """

    id: str
    name: str
    grade_start: int
    grade_end: int
    summary: str
    score: float
    provenance: Provenance


class TopicDetail(Topic):
    """完整节点。

    只对 `Topic` 做一处收窄：`provenance` 不再可空。缺失时补一个
    `unreviewed` / `confidence=0.0` 的默认值 —— 返回 null 会被消费端读成
    "没这回事"，而真实语义是"未经审核"，这两者差别很大。
    """

    provenance: Provenance


class MisconceptionHit(BaseModel):
    """一条误概念 + 它挂在哪个知识点上。

    `statement` / `probe` / `correction_hint` 三件套一个都不能少：
    少了 probe，agent 没法确认孩子是不是真的这么想；少了 correction_hint，
    它只知道"错了"而不知道往哪儿引。这个工具是整套设计里唯一为
    `misconceptions` 字段专门存在的，它有没有被用起来，是那个字段
    兑没兑现承诺的直接证据。
    """

    topic_id: str
    topic_name: str
    grade_start: int
    grade_end: int
    statement: str
    probe: str
    correction_hint: str
    score: float
    provenance: Provenance


class PrerequisiteEdge(BaseModel):
    """一条前置边，带它是从哪个节点往上走出来的。

    `reason` 原样透出（不摘要）：它是写给 LLM 直接拿去讲"为什么要先学这个"的，
    这一句正是 `dependencies` 相对"一条无标注的边"的全部增量。
    """

    topic_id: str
    name: str
    grade_start: int
    grade_end: int
    strength: str
    reason: str
    depth: int
    required_by: str
    provenance: Provenance


class RevisitNote(BaseModel):
    """螺旋边在规划视角下的样子。

    `direction` 是**相对当前这一步**说的：`later` 表示对方是同一概念更靠后的
    那一轮。规划时这条信息的用处很具体 —— 告诉家长"这个现在只到这个程度，
    五年级还会再来一轮"，而不是让他以为一次就学完了。
    """

    counterpart_id: str
    counterpart_name: str
    direction: str
    note: str


class PlanStep(BaseModel):
    order: int
    topic_id: str
    name: str
    grade_start: int
    grade_end: int
    summary: str
    provenance: Provenance
    revisits: list[RevisitNote] = []


class PlanResult(BaseModel):
    """一条学习路径。

    `skipped_known` 必须回传：`known_ids` 的语义是"连同上游一起剔除"，
    到底剔掉了什么只有这里说得清 —— 否则消费端只看到一条变短的路径，
    没法判断是它自己传对了还是工具吃掉了什么。
    """

    target_id: str
    steps: list[PlanStep]
    skipped_known: list[str]
    has_cycle: bool


class GraphStats(BaseModel):
    """图的规模与形状。

    存在的理由很实际：**防止 agent 在一张 41% 孤立、最长前置链只有 3 层的
    图上假装自己在做课程规划**。`isolated_topic_count` 与
    `longest_prerequisite_chain` 就是为此而报 —— 只报节点数会让一张
    孤儿遍地的图看起来很像一张图。
    """

    topic_count: int
    dependency_count: int
    hard_dependency_count: int
    soft_dependency_count: int
    revisit_count: int
    grade_distribution: dict[int, int]
    standard_codes: dict[str, list[str]]
    isolated_topic_count: int
    longest_prerequisite_chain: int
    has_cycle: bool
    review_status_counts: dict[str, int]


def _default_provenance() -> Provenance:
    return Provenance(method="unknown")


def load_graph(path: Path) -> CurriculumGraph:
    """读图并校验。

    校验失败抛 pydantic `ValidationError`（`ValueError` 子类）——
    坏数据要在 server 启动时炸，而不是等某个工具被调用时才炸。
    """
    return CurriculumGraph.model_validate(json.loads(Path(path).read_text(encoding="utf-8")))


class GraphIndex:
    """一次建索引，六个工具共用。

    建索引这件事本身要能扛住图的病态形状（环、孤儿、同名、空图）：
    这里只做纯粹的分组，不做任何"图应该是 DAG"的假设 —— 那些假设
    属于校验层，而我们读的是 `review_status=unreviewed` 的图。
    """

    def __init__(self, graph: CurriculumGraph, *, scorer: Scorer | None = None) -> None:
        self.graph = graph
        self.scorer: Scorer = scorer or LiteralScorer()
        self.by_id: dict[str, Topic] = {t.id: t for t in graph.topics}

        self._prereqs: dict[str, list[Dependency]] = {}
        self._dependents: dict[str, list[Dependency]] = {}
        for d in graph.dependencies:
            self._prereqs.setdefault(d.topic_id, []).append(d)
            self._dependents.setdefault(d.prerequisite_id, []).append(d)

        self._revisits: dict[str, list[Revisit]] = {}
        for r in graph.revisits:
            self._revisits.setdefault(r.earlier_id, []).append(r)
            self._revisits.setdefault(r.later_id, []).append(r)

        # 原来这里预算 grams；现在只存原文，预处理交给打分器
        self._search_text: dict[str, tuple[str, str]] = {
            t.id: (t.name, t.description) for t in graph.topics
        }
        self.scorer.warm(self._corpus_texts())

    def _corpus_texts(self) -> list[str]:
        """全部可被检索到的文本。向量打分器靠它一次编码完语料。"""
        texts: list[str] = []
        for t in self.graph.topics:
            texts.extend([t.name, t.description])
            for m in t.misconceptions:
                texts.extend([m.statement, m.probe, m.correction_hint])
        return texts

    def prerequisites_of(self, topic_id: str) -> list[Dependency]:
        """topic_id 的直接前置边（不含传递闭包）。"""
        return self._prereqs.get(topic_id, [])

    def dependents_of(self, topic_id: str) -> list[Dependency]:
        return self._dependents.get(topic_id, [])

    def search_texts(self, topic_id: str) -> tuple[str, str]:
        """(name, description) 原文。打分器自己决定要不要预处理。"""
        return self._search_text[topic_id]

    def revisits_of(self, topic_id: str) -> list[Revisit]:
        """所有一端是 topic_id 的螺旋边，不分方向。"""
        return self._revisits.get(topic_id, [])

    def require(self, topic_id: str) -> Topic:
        try:
            return self.by_id[topic_id]
        except KeyError:
            raise TopicNotFoundError(f"图里没有 topic_id={topic_id!r}") from None


def _summarize(text: str) -> str:
    return text if len(text) <= SUMMARY_CHARS else text[:SUMMARY_CHARS] + "…"


def _card(topic: Topic, score: float) -> TopicCard:
    return TopicCard(
        id=topic.id,
        name=topic.name,
        grade_start=topic.grade_start,
        grade_end=topic.grade_end,
        summary=_summarize(topic.description),
        score=round(score, 4),
        provenance=topic.provenance or _default_provenance(),
    )


def get_topic(index: GraphIndex, topic_id: str) -> TopicDetail:
    """取完整节点，含 evidence / assessment_prompt / misconceptions。"""
    topic = index.require(topic_id)
    return TopicDetail(
        **topic.model_dump(exclude={"provenance"}),
        provenance=topic.provenance or _default_provenance(),
    )


def search_topics(
    index: GraphIndex,
    query: str,
    *,
    grade: int | None = None,
    limit: int = 5,
) -> list[TopicCard]:
    """按字面相似度检索知识点，命中不了就返回空。

    宁可返回空也不硬凑一个：消费端 agent 拿到空结果会换词重试，
    拿到一个牵强的候选却可能当真 —— 后者的错误代价大得多。
    """
    hits: list[tuple[float, str, Topic]] = []
    for topic in index.graph.topics:
        if grade is not None and not (topic.grade_start <= grade <= topic.grade_end):
            continue
        name, description = index.search_texts(topic.id)
        name_hit = index.scorer.relevance(query, name)
        desc_hit = index.scorer.relevance(query, description)
        if max(name_hit, desc_hit) < index.scorer.min_relevance:
            continue
        hits.append((max(name_hit, DESCRIPTION_WEIGHT * desc_hit), topic.id, topic))

    hits.sort(key=lambda h: (-h[0], h[1]))
    return [_card(topic, score) for score, _, topic in hits[:limit]]


def match_misconceptions(
    index: GraphIndex, observation: str, *, limit: int = 5
) -> list[MisconceptionHit]:
    """把"孩子说了什么"对到图里的误概念上。

    匹配面是 statement（主）+ probe / correction_hint（次）。把后两者也算进来
    是因为家长的转述未必贴着 statement 的措辞，而 correction_hint 里往往
    带着这个错误的**术语说法**（"数位对齐"），那正是另一种问法的入口。
    """
    hits: list[tuple[float, str, int, MisconceptionHit]] = []

    for topic in index.graph.topics:
        for order, mis in enumerate(topic.misconceptions):
            primary = index.scorer.relevance(observation, mis.statement)
            secondary = max(
                index.scorer.relevance(observation, mis.probe),
                index.scorer.relevance(observation, mis.correction_hint),
            )
            if max(primary, secondary) < index.scorer.min_relevance:
                continue
            score = max(primary, SECONDARY_WEIGHT * secondary)
            hits.append(
                (
                    score,
                    topic.id,
                    order,
                    MisconceptionHit(
                        topic_id=topic.id,
                        topic_name=topic.name,
                        grade_start=topic.grade_start,
                        grade_end=topic.grade_end,
                        statement=mis.statement,
                        probe=mis.probe,
                        correction_hint=mis.correction_hint,
                        score=round(score, 4),
                        provenance=topic.provenance or _default_provenance(),
                    ),
                )
            )

    hits.sort(key=lambda h: (-h[0], h[1], h[2]))
    return [hit for _, _, _, hit in hits[:limit]]


def get_prerequisites(
    index: GraphIndex,
    topic_id: str,
    *,
    depth: int = 2,
    include_soft: bool = True,
) -> list[PrerequisiteEdge]:
    """逐层往上走前置边。

    `include_soft` 单独开关：hard 是"必须先会"，soft 是"最好先会"。
    要不要把 soft 算进来是产品决策，留在消费端而不是焊死在工具里。

    默认 `depth=2`（直接前置 + 前置的前置）：够 agent 讲清"为什么先学这个"，
    又不至于把整条闭包倒给它。要全量走到底用 `plan_path`。
    """
    index.require(topic_id)

    seen = {topic_id}
    edges: list[PrerequisiteEdge] = []
    frontier = [topic_id]

    for level in range(1, depth + 1):
        next_frontier: list[str] = []
        for current in frontier:
            for d in index.prerequisites_of(current):
                if not include_soft and d.strength == "soft":
                    continue
                prereq = index.by_id.get(d.prerequisite_id)
                # 悬挂引用由校验层报告；暴露层的职责是别把它变成一次崩溃
                if prereq is None or prereq.id in seen:
                    continue
                seen.add(prereq.id)
                next_frontier.append(prereq.id)
                edges.append(
                    PrerequisiteEdge(
                        topic_id=prereq.id,
                        name=prereq.name,
                        grade_start=prereq.grade_start,
                        grade_end=prereq.grade_end,
                        strength=d.strength,
                        reason=d.reason,
                        depth=level,
                        required_by=current,
                        provenance=prereq.provenance or _default_provenance(),
                    )
                )
        frontier = next_frontier
        if not frontier:
            break

    return edges


def _ancestors(index: GraphIndex, topic_id: str, include_soft: bool) -> set[str]:
    """topic_id 及其全部上游前置。`seen` 兼作环的刹车。"""
    seen = {topic_id}
    stack = [topic_id]
    while stack:
        current = stack.pop()
        for d in index.prerequisites_of(current):
            if not include_soft and d.strength == "soft":
                continue
            pid = d.prerequisite_id
            if pid in index.by_id and pid not in seen:
                seen.add(pid)
                stack.append(pid)
    return seen


def _topological_order(
    index: GraphIndex, nodes: set[str], include_soft: bool
) -> tuple[list[str], bool]:
    """Kahn 拓扑排序，同层按 (年级, id) 定序。

    按年级打破平局不只是为了确定性：并列可学的两个节点里，先排低年级的
    才符合"按课标顺序补"的直觉。

    环上的节点谁也进不了队列，此时**照样把它们排出来并把 `has_cycle` 置真**
    —— 返回半条路径会让消费端以为剩下的部分不需要学，比给出一个带标记的
    可疑顺序更危险。
    """
    indegree = {
        n: sum(
            1
            for d in index.prerequisites_of(n)
            if d.prerequisite_id in nodes and (include_soft or d.strength == "hard")
        )
        for n in nodes
    }

    def sort_key(node_id: str) -> tuple[int, str]:
        topic = index.by_id[node_id]
        return (topic.grade_start, node_id)

    ready = sorted((n for n, deg in indegree.items() if deg == 0), key=sort_key)
    ordered: list[str] = []
    while ready:
        current = ready.pop(0)
        ordered.append(current)
        for d in index.dependents_of(current):
            dependent = d.topic_id
            if dependent not in indegree or (not include_soft and d.strength == "soft"):
                continue
            indegree[dependent] -= 1
            if indegree[dependent] == 0:
                ready.append(dependent)
                ready.sort(key=sort_key)

    leftover = sorted(nodes - set(ordered), key=sort_key)
    return ordered + leftover, bool(leftover)


def _revisit_notes(index: GraphIndex, topic_id: str) -> list[RevisitNote]:
    notes: list[RevisitNote] = []
    for r in index.revisits_of(topic_id):
        counterpart_id = r.later_id if r.earlier_id == topic_id else r.earlier_id
        counterpart = index.by_id.get(counterpart_id)
        if counterpart is None:
            continue
        notes.append(
            RevisitNote(
                counterpart_id=counterpart_id,
                counterpart_name=counterpart.name,
                direction="later" if r.earlier_id == topic_id else "earlier",
                note=r.note,
            )
        )
    return notes


def plan_path(
    index: GraphIndex,
    target_id: str,
    *,
    known_ids: list[str] | tuple[str, ...] = (),
    include_soft: bool = True,
) -> PlanResult:
    """排出学到 target 所需的学习序列，跳过已掌握的部分。

    **`known_ids` 的语义是定死的**：传入的 id 视为已掌握，它自己**和它的
    全部上游前置**一并剔除（"会了 X"蕴含"会了 X 的前置"）。另一种读法
    ——只剔除 id 本身、上游照留——会在孩子已经会分数四则时还让他从整数
    加法学起。若真需要那种行为，那是另一个参数，不是这个参数的另一种解释
    （论证见 docs/mcp-server-design.md §3）。

    id 写错时抛错而不是静默忽略：静默会让"到底跳过了什么"变成猜谜。
    """
    index.require(target_id)
    for known in known_ids:
        index.require(known)

    closure = _ancestors(index, target_id, include_soft)
    mastered: set[str] = set()
    for known in known_ids:
        mastered |= _ancestors(index, known, include_soft)

    remaining = closure - mastered
    ordered, has_cycle = _topological_order(index, remaining, include_soft)

    steps = []
    for order, node_id in enumerate(ordered, start=1):
        topic = index.by_id[node_id]
        steps.append(
            PlanStep(
                order=order,
                topic_id=topic.id,
                name=topic.name,
                grade_start=topic.grade_start,
                grade_end=topic.grade_end,
                summary=_summarize(topic.description),
                provenance=topic.provenance or _default_provenance(),
                revisits=_revisit_notes(index, topic.id),
            )
        )

    return PlanResult(
        target_id=target_id,
        steps=steps,
        skipped_known=sorted(closure & mastered),
        has_cycle=has_cycle,
    )


def _longest_chain_and_cycle(index: GraphIndex) -> tuple[int, bool]:
    """最长前置链的节点数，以及路上有没有撞见环。

    环不是假设情形：我们读的是 `review_status=unreviewed` 的图，校验层
    只报告环、不阻止它落盘。撞见回边时把它当作深度 0 截断 —— 结果不精确，
    但这个函数的职责是给出规模量级，不是求解带环图上的最长路（那是 NP-hard）。
    """
    depth_of: dict[str, int] = {}
    on_stack: set[str] = set()
    has_cycle = False

    def depth(tid: str) -> int:
        nonlocal has_cycle
        if tid in depth_of:
            return depth_of[tid]
        if tid in on_stack:
            has_cycle = True
            return 0
        on_stack.add(tid)
        best = 0
        for d in index.prerequisites_of(tid):
            if d.prerequisite_id in index.by_id:  # 悬挂引用交给校验层报，这里跳过
                best = max(best, depth(d.prerequisite_id))
        on_stack.discard(tid)
        depth_of[tid] = best + 1
        return depth_of[tid]

    longest = max((depth(t.id) for t in index.graph.topics), default=0)
    return longest, has_cycle


def get_graph_stats(index: GraphIndex) -> GraphStats:
    """图有多大、多稀、多没审核 —— 一次说清。"""
    graph = index.graph

    grade_distribution: dict[int, int] = {}
    standard_codes: dict[str, set[str]] = {}
    review_status_counts: dict[str, int] = {}
    for t in graph.topics:
        for g in range(t.grade_start, t.grade_end + 1):
            grade_distribution[g] = grade_distribution.get(g, 0) + 1
        for s in t.standards:
            standard_codes.setdefault(s.curriculum, set()).add(s.code)
        status = (t.provenance or _default_provenance()).review_status
        review_status_counts[status] = review_status_counts.get(status, 0) + 1

    connected: set[str] = set()
    for d in graph.dependencies:
        connected.update({d.topic_id, d.prerequisite_id})
    for r in graph.revisits:
        connected.update({r.earlier_id, r.later_id})

    longest, has_cycle = _longest_chain_and_cycle(index)

    return GraphStats(
        topic_count=len(graph.topics),
        dependency_count=len(graph.dependencies),
        hard_dependency_count=sum(1 for d in graph.dependencies if d.strength == "hard"),
        soft_dependency_count=sum(1 for d in graph.dependencies if d.strength == "soft"),
        revisit_count=len(graph.revisits),
        grade_distribution=dict(sorted(grade_distribution.items())),
        standard_codes={c: sorted(codes) for c, codes in sorted(standard_codes.items())},
        isolated_topic_count=sum(1 for t in graph.topics if t.id not in connected),
        longest_prerequisite_chain=longest,
        has_cycle=has_cycle,
        review_status_counts=review_status_counts,
    )
