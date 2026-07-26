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
from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, TopicDraft


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
) -> CurriculumGraph:
    """组装成对外 schema。假定调用方（Task 8 编排层）已保证 edges 只引用
    drafts 里仍存活的 id —— review_edges 之前会先用 kept_ids 过滤一轮，
    review_edges 本身只会再收窄，不会新增引用。若这个前置条件被违反，
    说明调用方状态不同步，直接报错而非静默丢边（见下方两处检查）。
    """
    provenance = Provenance(
        method=f"llm-extract/{model_id}",
        review_status="unreviewed",
        # 没有任何教研审核，非零置信度都是自欺。投票结果单独记进
        # review-log.json —— 模型间的一致程度和教研正确性是两件事。
        confidence=0.0,
    )

    topic_id_by_draft: dict[str, str] = {}
    # 碰撞检查专用集合：用 set 做 O(1) 成员测试，而不是每次现算
    # `dict.values()`（O(n) 线性扫描）—— 原实现在 n 个 draft 上是 O(n²)。
    seen_topic_ids: set[str] = set()
    topics: list[Topic] = []
    for draft in drafts:
        topic_id = make_topic_id(draft.content)
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
                provenance=provenance,
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
