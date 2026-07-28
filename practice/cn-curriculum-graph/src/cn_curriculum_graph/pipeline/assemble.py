"""把流水线内部类型翻译成对外 schema。

这一层负责补齐**代码该填的字段**：id、provenance、standards[].curriculum。
理由见 docs/pipeline-design.md §5 —— 模型只产出它有资格产出的东西。
"""

from __future__ import annotations

import hashlib

from cn_curriculum_graph.models import (
    CurriculumGraph,
    Dependency,
    Provenance,
    Standard,
    Topic,
)
from cn_curriculum_graph.pipeline.models import DraftContent, DropRecord, ProposedEdge, TopicDraft
from cn_curriculum_graph.pipeline.topic_registry import TopicRegistry, assign_topic_ids

_STRENGTH_RANK = {"soft": 0, "hard": 1}


def dedupe_edges_by_pair(
    kept_edges: dict[str, list[ProposedEdge]],
) -> tuple[dict[str, list[ProposedEdge]], list[DropRecord]]:
    """待裁决 #5 的修复：同一 (target_draft_id, prerequisite_draft_id) 若给出
    多条边（典型情形是 hard + soft 各一条），不去重就都会进 graph.json 的
    dependencies —— 这是对外产物的正确性缺陷，下游按边计数或建图的消费方
    都会读错。

    去重规则：hard 胜 soft（强依赖优先）；同强度保留先出现的。

    **为什么不放进 assemble() 内部、而是单独一个函数**：assemble() 的签名是
    `-> CurriculumGraph`，没有 DropRecord 通道，且它的既有设计哲学是"调用方
    负责保证前置条件、assemble 只管报错，不做静默修正"（见 assemble() 里
    "边引用未知 draft id 直接 raise" 的注释）。把去重悄悄塞进 assemble 内部、
    不留痕迹，等于又制造一个"没有留痕的丢弃"，正是本项目要修的第一类问题。
    所以选择在编排层（pipeline/run.py）调用 assemble 之前，用这个独立的纯
    函数做去重并把丢弃的那条记成 DropRecord——assemble() 签名保持不变，
    调用方（编排层）继续对"边已去重"这个前置条件负责。

    在 draft_id 层面去重而非 assemble 内部的 topic_id 层面：两者等价（同一
    draft_id 到同一 topic_id 是确定性映射），但在 draft_id 层面能在调用
    assemble 之前就拿到 DropRecord，不用等 assemble 算出 topic_id 之后才能
    定位。
    """
    deduped: dict[str, list[ProposedEdge]] = {}
    drops: list[DropRecord] = []

    for target_id, group in kept_edges.items():
        by_prereq: dict[str, ProposedEdge] = {}
        for edge in group:
            prereq_id = edge.prerequisite_draft_id
            pair_ref = f"{target_id}<-{prereq_id}"
            existing = by_prereq.get(prereq_id)
            if existing is None:
                by_prereq[prereq_id] = edge
                continue
            if _STRENGTH_RANK[edge.strength] > _STRENGTH_RANK[existing.strength]:
                # 新出现的更强（hard 胜 soft）—— 替换，丢弃原来那条较弱的。
                # detail 带上被丢弃那条边自己的 reason：同一 (target, prerequisite)
                # 上若出现 ≥3 条边，多条 DropRecord 的 (stage, ref, reason, detail)
                # 四元组不能因此撞车，否则会被 io.append_drops 的全字段去重
                # 误判成"同一条记录写了两次"，把两次真实丢弃静默合并成一次。
                drops.append(
                    DropRecord(
                        stage="assemble",
                        ref=pair_ref,
                        reason="DUPLICATE_EDGE",
                        detail=(
                            f"重复边：保留 strength={edge.strength}（更强），"
                            f"丢弃 strength={existing.strength}"
                            f"（原因：{existing.reason}）"
                        ),
                    )
                )
                by_prereq[prereq_id] = edge
            else:
                # 同强度或原来的更强 —— 保留先出现的，丢弃这条。
                # 同上，detail 带上被丢弃这条边自己的 reason 以便与其他丢弃事件区分。
                drops.append(
                    DropRecord(
                        stage="assemble",
                        ref=pair_ref,
                        reason="DUPLICATE_EDGE",
                        detail=(
                            f"重复边：保留先出现的 strength={existing.strength}，"
                            f"丢弃 strength={edge.strength}"
                            f"（原因：{edge.reason}）"
                        ),
                    )
                )
        deduped[target_id] = list(by_prereq.values())

    return deduped, drops


def make_topic_id(content: DraftContent) -> str:
    """确定性 id：重排输入不改变结果，便于比对两次跑的差异。

    取 (name, domain, grade_start) 而非序号，是为了让 id 在增删条目时保持稳定。
    """
    seed = f"{content.name}|{content.domain}|{content.grade_start}"
    return "t_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


def assemble(
    drafts: list[TopicDraft],
    edges: dict[str, list[ProposedEdge]],
    model_id: str,
    curriculum: str,
    registry: TopicRegistry | None = None,
) -> CurriculumGraph:
    """组装成对外 schema。假定调用方（Task 8 编排层）已保证 edges 只引用
    drafts 里仍存活的 id —— review_edges 之前会先用 kept_ids 过滤一轮，
    review_edges 本身只会再收窄，不会新增引用。若这个前置条件被违反，
    说明调用方状态不同步，直接报错而非静默丢边（见下方两处检查）。
    """
    # 传了注册表就走"认领"路径（同义改写不换身份，见 topic_registry 模块文档）；
    # 不传就退回原来的纯哈希 —— 大量既有测试不关心身份稳定性，不该为此都改一遍。
    if registry is not None:
        assigned_ids = assign_topic_ids([d.content for d in drafts], registry)
    else:
        assigned_ids = [make_topic_id(d.content) for d in drafts]

    topic_id_by_draft: dict[str, str] = {}
    # 碰撞检查专用集合：用 set 做 O(1) 成员测试，而不是每次现算
    # `dict.values()`（O(n) 线性扫描）—— 原实现在 n 个 draft 上是 O(n²)。
    seen_topic_ids: set[str] = set()
    topics: list[Topic] = []
    for draft, topic_id in zip(drafts, assigned_ids, strict=True):
        if topic_id in seen_topic_ids:
            raise ValueError(
                f"id 碰撞：{topic_id}（{draft.content.name}）—— "
                "名称、领域、起始年级三项全同，本该在 dedupe 阶段合并"
            )
        seen_topic_ids.add(topic_id)
        topic_id_by_draft[draft.draft_id] = topic_id
        topics.append(
            Topic(
                id=topic_id,
                name=draft.content.name,
                description=draft.content.description,
                type=draft.content.type,
                subject=draft.content.subject,
                domain=draft.content.domain,
                grade_start=draft.content.grade_start,
                grade_end=draft.content.grade_end,
                evidence=draft.content.evidence,
                assessment_prompt=draft.content.assessment_prompt,
                misconceptions=draft.content.misconceptions,
                standards=[Standard(curriculum=curriculum, code=c) for c in draft.standard_codes],
                # 每个 topic 独立构造一份 Provenance，不能挪到循环外共享同一个
                # 对象引用 —— pydantic 模型默认可变，届时逐节点写回审核结果
                # （ReviewStatus 预留了 self_reviewed / expert_reviewed）会静默
                # 污染全部节点，正好毁掉"每节点独立记录生成方式与审核状态"这个
                # 本项目相对 Marble 的刻意差异点（见 models.py 模块 docstring）。
                provenance=Provenance(
                    method=f"llm-extract/{model_id}",
                    review_status="unreviewed",
                    # 没有任何教研审核，非零置信度都是自欺。投票结果单独记进
                    # review-log.json —— 模型间的一致程度和教研正确性是两件事。
                    confidence=0.0,
                ),
            )
        )

    # `edges` 引用 drafts 里已不存在的 id，意味着调用方状态不同步（例如
    # 传入的 drafts 是 review 淘汰后的子集，edges 却还留着淘汰前的引用）。
    # 这与 Task 4/5/6 反复踩到的"另一份状态被改写后索引失效"是同一类问题。
    # 本项目的原则是"没有静默跳过"：assemble 是纯规则的最后一层，没有
    # 下一层再替它兜底，此刻默不作声地丢边，图就永久少了这条边而无人知晓。
    # 所以这里选择直接报错，而不是像 review_edges 那样记 DropRecord 后跳过
    # ——两者面对的是不同处境：review_edges 之前，"部分候选被淘汰"是预期
    # 中的常态，DropRecord 记的是正常业务结果；这里则是调用契约被违反，
    # 报错能让问题在离源头最近的地方暴露，而不是拖到 run_all 校验才发现
    # 图不完整。
    dependencies: list[Dependency] = []
    for target_draft_id, proposed in edges.items():
        if target_draft_id not in topic_id_by_draft:
            raise ValueError(
                f"assemble 收到未知的 draft id：{target_draft_id} —— "
                "不在传入的 drafts 列表中（可能已被上游 dedupe/review 淘汰），"
                "调用方必须保证 edges 只引用仍存活的 draft"
            )
        for edge in proposed:
            if edge.prerequisite_draft_id not in topic_id_by_draft:
                raise ValueError(
                    f"assemble 收到未知的 draft id：{edge.prerequisite_draft_id} —— "
                    f"是 {target_draft_id} 提议的前置，但不在传入的 drafts 列表中"
                    "（可能已被上游 dedupe/review 淘汰），调用方必须保证 edges "
                    "只引用仍存活的 draft"
                )
            dependencies.append(
                Dependency(
                    topic_id=topic_id_by_draft[target_draft_id],
                    prerequisite_id=topic_id_by_draft[edge.prerequisite_draft_id],
                    strength=edge.strength,
                    reason=edge.reason,
                )
            )

    return CurriculumGraph(topics=topics, dependencies=dependencies)
