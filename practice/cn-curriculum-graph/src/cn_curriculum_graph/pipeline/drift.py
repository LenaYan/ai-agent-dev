"""两次重跑之间的漂移度量：身份稳不稳，**以及内容还在不在**。

## 它解决什么

`topic_registry` 让节点 id 活过了重跑，但它只稳**身份**，不稳**内容**。
2026-07-29 那轮重跑里发现的具体形状是：

    「加法交换律」节点还在（id 认领成功），
    但它的误概念少了「对减法也成立」那一条。

**节点在不等于误概念在。** 而误概念是这张图对诊断类应用的全部价值所在
（`models.Misconception` 的模块文档：有了它才能从"孩子答错了"推进到
"孩子为什么这么想"）。身份留存率报 70% 而误概念悄悄掉了一半，下游看到的
是"图很稳"，实际拿到的是空壳。这一维至今没有任何数据 —— 这个模块就是去量它。

## 为什么两层指标必须分开算

身份层（id / 名称 Jaccard）和内容层（误概念留存）**分母不同**：
误概念留存只在"两轮都存在的同一个 id"上才有定义 —— 节点都换身份了，
说它的误概念丢了没有意义（那是身份问题，已经被上一层记过一次账）。
混在一起算会让一个数同时背两件事，正是本项目反复踩的那种"度量缺陷
冒充质量问题"。所以：**身份没留住的节点不进内容层的分母**，与
`eval_diagnosis.py` 里"空样本不进召回分母"是同一条纪律。

## 为什么"精确留存"与"含改写留存"要分开报

整个项目在名称上已经吃过一次亏：**看起来的动荡里约七成不是内容变了，
是措辞变了**（`docs/pipeline-reproducibility.md`）。误概念是 LLM 生成的
整句自然语言，比名称更长、更容易被改写，只报字符串精确留存必然把改写
算成丢失，得出一个虚高的"内容抖动"。

但反过来只报模糊留存也不行 —— 那会把"改写"和"没变"糊成一团，而
「statement 被重写」对下游是有代价的：`data/diagnosis-eval-groundtruth.json`
的观察句是照着 statement 的措辞挂的，statement 一改，检索命中就可能变。

所以两个数一起报，**它们的差就是改写的量**，谁也不冒充谁。

## 阈值是未标定的旋钮，不要当成结论

`DEFAULT_MATCH_THRESHOLD` 与 `topic_registry.CLAIM_THRESHOLD` 不同：
后者是在三次真实运行上量出来的，前者**没有任何实测支撑** —— 本机
`data/generated/` 是 gitignore 的，写这个模块时手上一张真图都没有。

因此 `retention_curve` 存在：报一整条曲线而不是单点，让"改写留存率"
不被一个拍脑袋的数字绑架。等真图到手，按曲线的形状（以及逐条读配对
明细）再定这个数。**在那之前，任何单点留存率都只是那个阈值下的读数。**
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Sequence

from cn_curriculum_graph.models import CurriculumGraph, Misconception, Topic
from cn_curriculum_graph.serve.scoring import LiteralScorer, normalize_math

# 两条 statement 的字面相关度到这个数，才算"同一条误概念被改写了"。
# **未标定** —— 理由见模块文档。取 0.6 只是与 `CLAIM_THRESHOLD` 保持同一
# 量级，方便对读，不代表它在误概念这种长句上同样合适（长句的 bigram 基数
# 更大，通用措辞更多，很可能该定得更高）。
DEFAULT_MATCH_THRESHOLD = 0.6

_scorer = LiteralScorer()


def _exact_key(statement: str) -> str:
    """判"原文没变"用的键：去空白、统一大小写、数学表达归一化。

    **不能用"相关度 == 1.0"代替字符串相等**：`LiteralScorer` 的相关度是
    双向覆盖率取大者，对**包含关系**会给满分（"计数单位的感悟" ⊃ "计数单位"
    实测就是 1.00）。用满分当"原文相同"，会把一次真实的截短算成没变。
    """
    return "".join(ch for ch in normalize_math(statement).lower() if not ch.isspace())


@dataclass(frozen=True)
class MatchedPair:
    """一条误概念在两轮之间的对应关系。"""

    before: str
    after: str
    score: float

    @property
    def is_verbatim(self) -> bool:
        return _exact_key(self.before) == _exact_key(self.after)


@dataclass
class NodeDrift:
    """单个节点（id 两轮都在）的误概念漂移。"""

    topic_id: str
    name_before: str
    name_after: str
    matched: list[MatchedPair] = field(default_factory=list)
    lost: list[str] = field(default_factory=list)
    """上一轮有、这一轮**连改写都对不上**的 statement —— 真正的内容丢失。"""
    added: list[str] = field(default_factory=list)

    @property
    def renamed(self) -> bool:
        return self.name_before != self.name_after

    @property
    def before_count(self) -> int:
        return len(self.matched) + len(self.lost)

    @property
    def verbatim_count(self) -> int:
        return sum(1 for p in self.matched if p.is_verbatim)

    @property
    def is_hollowed(self) -> bool:
        """身份留住了、误概念全丢了 —— 「节点在不等于误概念在」的极端形态。

        上一轮本来就没有误概念的节点不算空壳：它没有可丢的东西，
        算进去只会稀释这个数。
        """
        return self.before_count > 0 and not self.matched


def _score_matrix(
    before: Sequence[Misconception], after: Sequence[Misconception]
) -> list[list[float]]:
    """statement 两两相关度。

    **只比 statement，不比 probe / correction_hint**：statement 是"孩子怎么想"，
    是这条误概念的身份；另外两个字段是围绕它派生的教学动作，会随措辞一起
    重写。把三段拼起来算相关度，等于让派生内容替身份投票 —— 与边审那次
    「拿待验证的说法去认定待验证的对象」是同一类错误。
    """
    return [[_scorer.relevance(b.statement, a.statement) for a in after] for b in before]


def _greedy_pairs(matrix: list[list[float]], threshold: float) -> dict[int, tuple[int, float]]:
    """贪心一对一配对，按相关度从高到低认领。

    与 `topic_registry.assign_topic_ids` 同一套约束和同一个理由：没有
    一对一，一条被拆成两条的误概念会双双认领同一个前身，留存率就被
    重复计数顶高 —— 那正是这个模块要防的虚高。

    排序键带上下标，让同分时的先后确定，避免同一份输入两次跑给出不同配对。
    """
    candidates = [
        (score, i, j)
        for i, row in enumerate(matrix)
        for j, score in enumerate(row)
        if score >= threshold
    ]
    pairs: dict[int, tuple[int, float]] = {}
    taken: set[int] = set()
    for score, i, j in sorted(candidates, key=lambda t: (-t[0], t[1], t[2])):
        if i in pairs or j in taken:
            continue
        pairs[i] = (j, score)
        taken.add(j)
    return pairs


def diff_misconceptions(
    before: Sequence[Misconception],
    after: Sequence[Misconception],
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
    matrix: list[list[float]] | None = None,
) -> tuple[list[MatchedPair], list[str], list[str]]:
    """配对两轮的误概念，返回 (matched, lost, added)。

    `matrix` 可传入预算好的相关度矩阵 —— 扫阈值曲线时同一对节点会被算
    多次，重算矩阵会让扫描变成 N 倍的字符串运算。
    """
    matrix = _score_matrix(before, after) if matrix is None else matrix
    pairs = _greedy_pairs(matrix, threshold)
    matched = [
        MatchedPair(before=before[i].statement, after=after[j].statement, score=score)
        for i, (j, score) in sorted(pairs.items())
    ]
    lost = [m.statement for i, m in enumerate(before) if i not in pairs]
    claimed = {j for j, _ in pairs.values()}
    added = [m.statement for j, m in enumerate(after) if j not in claimed]
    return matched, lost, added


@dataclass
class DriftReport:
    """一次「上一轮 → 这一轮」的完整漂移读数。"""

    threshold: float
    ids_before: set[str]
    ids_after: set[str]
    names_before: set[str]
    names_after: set[str]
    nodes: list[NodeDrift] = field(default_factory=list)
    """只含 id 两轮都在的节点 —— 内容层的分母，理由见模块文档。"""

    # ---- 身份层 ----

    @property
    def id_jaccard(self) -> float:
        return _jaccard(self.ids_before, self.ids_after)

    @property
    def name_jaccard(self) -> float:
        return _jaccard(self.names_before, self.names_after)

    @property
    def survived_ids(self) -> set[str]:
        return self.ids_before & self.ids_after

    @property
    def renamed_nodes(self) -> list[NodeDrift]:
        """id 留住了但名字变了 —— 注册表认领生效的直接证据。"""
        return [n for n in self.nodes if n.renamed]

    # ---- 内容层 ----

    @property
    def statements_before(self) -> int:
        return sum(n.before_count for n in self.nodes)

    @property
    def verbatim_retention(self) -> float:
        """原文一字未改的比例。"""
        return _ratio(sum(n.verbatim_count for n in self.nodes), self.statements_before)

    @property
    def matched_retention(self) -> float:
        """含改写的留存比例。与 `verbatim_retention` 的差 = 改写的量。"""
        return _ratio(
            sum(len(n.matched) for n in self.nodes), self.statements_before
        )

    @property
    def lost_statements(self) -> list[tuple[str, str, str]]:
        """(topic_id, 节点名, 丢掉的 statement)。**逐条看比看平均值有用。**"""
        return [(n.topic_id, n.name_after, s) for n in self.nodes for s in n.lost]

    @property
    def hollowed_nodes(self) -> list[NodeDrift]:
        return [n for n in self.nodes if n.is_hollowed]

    @property
    def median_matched_score(self) -> float:
        """配对上的那些 statement 的相关度中位数。

        与 `docs/pipeline-reproducibility.md` 那张表里的「误概念字面相关度
        中位数」同口径，方便跨轮对读。注意它**只统计配对成功的**，
        丢失的那些不在里面 —— 这个数偏高是设计如此，不是好消息。
        """
        scores = sorted(p.score for n in self.nodes for p in n.matched)
        if not scores:
            return 0.0
        mid = len(scores) // 2
        return scores[mid] if len(scores) % 2 else (scores[mid - 1] + scores[mid]) / 2


def _jaccard(a: set[str], b: set[str]) -> float:
    union = a | b
    return len(a & b) / len(union) if union else 0.0


def _ratio(num: int, den: int) -> float:
    return num / den if den else 0.0


def compare_graphs(
    before: CurriculumGraph,
    after: CurriculumGraph,
    *,
    threshold: float = DEFAULT_MATCH_THRESHOLD,
) -> DriftReport:
    """比两张图。`before` 是基线（上一轮），`after` 是新一轮。"""
    a_by_id = _by_id(before)
    b_by_id = _by_id(after)
    nodes = [
        _node_drift(a_by_id[tid], b_by_id[tid], threshold)
        for tid in sorted(a_by_id.keys() & b_by_id.keys())
    ]
    return DriftReport(
        threshold=threshold,
        ids_before=set(a_by_id),
        ids_after=set(b_by_id),
        names_before={t.name for t in before.topics},
        names_after={t.name for t in after.topics},
        nodes=nodes,
    )


def _by_id(graph: CurriculumGraph) -> dict[str, Topic]:
    """同 id 出现多次时保留最后一个（与 `CurriculumGraph.by_id` 一致）。

    图里本不该有重复 id（`validators/structure.py` 会拦），这里不额外报错：
    这个模块是度量工具，不是校验器，遇到病态输入应当照样出数而不是罢工。
    """
    return graph.by_id


def _node_drift(before: Topic, after: Topic, threshold: float) -> NodeDrift:
    matched, lost, added = diff_misconceptions(
        before.misconceptions, after.misconceptions, threshold=threshold
    )
    return NodeDrift(
        topic_id=before.id,
        name_before=before.name,
        name_after=after.name,
        matched=matched,
        lost=lost,
        added=added,
    )


@dataclass(frozen=True)
class CurvePoint:
    threshold: float
    matched_retention: float
    hollowed: int


def retention_curve(
    before: CurriculumGraph,
    after: CurriculumGraph,
    thresholds: Sequence[float],
) -> list[CurvePoint]:
    """同一对图上只挪配对阈值，报一整条留存曲线。

    **为什么这不是可有可无的附加功能**：`DEFAULT_MATCH_THRESHOLD` 没有实测
    支撑，单点留存率因此不可引用。曲线让读者看见"这个数对阈值有多敏感" ——
    如果 0.4 到 0.8 之间留存率几乎不动，那说明配对得很干脆（改写幅度小），
    单点可信；如果一路滑坡，说明大量配对卡在门槛附近，那个单点就是抽签。

    相关度矩阵按节点算一次、全阈值复用；否则扫 N 个阈值就是 N 遍全量
    字符串运算（同 `eval_diagnosis.sweep_thresholds` 复用 scorer 缓存的理由）。
    """
    a_by_id = _by_id(before)
    b_by_id = _by_id(after)
    shared = sorted(a_by_id.keys() & b_by_id.keys())
    cached = [
        (
            a_by_id[tid].misconceptions,
            b_by_id[tid].misconceptions,
            _score_matrix(a_by_id[tid].misconceptions, b_by_id[tid].misconceptions),
        )
        for tid in shared
    ]

    points = []
    for threshold in thresholds:
        total = matched_count = hollowed = 0
        for before_ms, after_ms, matrix in cached:
            matched, lost, _ = diff_misconceptions(
                before_ms, after_ms, threshold=threshold, matrix=matrix
            )
            total += len(matched) + len(lost)
            matched_count += len(matched)
            if before_ms and not matched:
                hollowed += 1
        points.append(
            CurvePoint(
                threshold=threshold,
                matched_retention=_ratio(matched_count, total),
                hollowed=hollowed,
            )
        )
    return points
