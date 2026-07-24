"""结构层校验：环、悬挂引用、孤立节点。"""

from collections import defaultdict

from cn_curriculum_graph.models import CurriculumGraph
from cn_curriculum_graph.validators.base import Finding, Severity


def check_no_dangling_refs(graph: CurriculumGraph) -> list[Finding]:
    """任何边的两端都必须是已定义的 topic。"""
    known = set(graph.by_id)
    findings: list[Finding] = []

    def report(missing: str, edge_kind: str, detail: str) -> None:
        findings.append(
            Finding(
                code="DANGLING_REF",
                severity=Severity.ERROR,
                message=f"{edge_kind} 引用了不存在的节点 {missing}（{detail}）",
                context={"missing_id": missing, "edge_kind": edge_kind},
            )
        )

    for d in graph.dependencies:
        detail = f"{d.topic_id} ← {d.prerequisite_id}"
        for ref in (d.topic_id, d.prerequisite_id):
            if ref not in known:
                report(ref, "dependency", detail)

    for r in graph.revisits:
        detail = f"{r.earlier_id} → {r.later_id}"
        for ref in (r.earlier_id, r.later_id):
            if ref not in known:
                report(ref, "revisit", detail)

    return findings


def check_no_isolated_topics(graph: CurriculumGraph) -> list[Finding]:
    """孤立节点通常是抽取流水线漏接边的信号，而不是真的没有关联。"""
    connected: set[str] = set()
    for d in graph.dependencies:
        connected.update({d.topic_id, d.prerequisite_id})
    for r in graph.revisits:
        connected.update({r.earlier_id, r.later_id})

    return [
        Finding(
            code="ISOLATED_TOPIC",
            severity=Severity.WARNING,
            message=f"节点 {t.id}（{t.name}）没有任何先修边或螺旋边",
            context={"topic_id": t.id},
        )
        for t in graph.topics
        if t.id not in connected
    ]


def check_no_cycles(graph: CurriculumGraph) -> list[Finding]:
    """先修边必须构成 DAG —— 有环意味着存在学不动的死循环。"""
    successors: dict[str, list[str]] = defaultdict(list)
    for d in graph.dependencies:
        successors[d.prerequisite_id].append(d.topic_id)

    findings: list[Finding] = []
    state: dict[str, int] = {}  # 0/未访问 1/在栈上 2/已完成
    seen_cycles: set[frozenset[str]] = set()

    def visit(node: str, stack: list[str]) -> None:
        state[node] = 1
        stack.append(node)
        for nxt in successors.get(node, []):
            if state.get(nxt, 0) == 1:
                cycle = stack[stack.index(nxt):]
                key = frozenset(cycle)
                if key not in seen_cycles:
                    seen_cycles.add(key)
                    findings.append(
                        Finding(
                            code="CYCLE",
                            severity=Severity.ERROR,
                            message="先修边中存在环：" + " → ".join(cycle + [nxt]),
                            context={"cycle": cycle},
                        )
                    )
            elif state.get(nxt, 0) == 0:
                visit(nxt, stack)
        stack.pop()
        state[node] = 2

    for t in graph.topics:
        if state.get(t.id, 0) == 0:
            visit(t.id, [])

    return findings
