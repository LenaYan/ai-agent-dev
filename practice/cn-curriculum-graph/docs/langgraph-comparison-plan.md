# LangGraph 重写 + 对比实验 实现计划

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用 LangGraph 重写生成流水线的编排层（六层纯函数不动），两个实现跑同一份素材、注入同样的故障，用数据写出手写 vs 框架的对比笔记。

**Architecture:** 共存而非替换。先修三个与框架无关的域缺陷（共用层受益），再做 A 阶段对等实现（一层一个 Node）拿基线数据，最后做 B 阶段 `Send` 扇出量额外收益。故障注入靠包裹已有的 `PipelineDeps`，零生产代码改动。

**Tech Stack:** Python 3.12、pydantic v2、langgraph 1.2.9、langgraph-checkpoint-sqlite 3.1.0、pytest、uv。

设计依据：`docs/langgraph-comparison-design.md`。"为什么这么设计"去那里找，本文只讲怎么做。

## Global Constraints

- 工作目录一律 `practice/cn-curriculum-graph/`，命令用 `uv run`
- 全程 TDD：先写失败测试 → 跑一次**亲眼看到 RED** → 最小实现 → 跑到 GREEN → 提交。**不许先写实现**
- **六层纯函数（chunk/extract/dedupe/edges/review/assemble 的核心逻辑）不改行为**，只允许 Task 1、2 明确列出的域缺陷修复
- **Node 里不许写业务逻辑**：每个 Node 只做「从 state 取输入 → 调那层已有函数 → 返回 delta」。写了业务逻辑，对比就变成"手写版 vs 框架版+重构"，不公平
- 中文注释与文档字符串，术语保留英文原词
- LangGraph 节点签名是 `(state, runtime)`，运行时依赖走 `runtime.context`
- 现有全 fake 端到端测试（含跨层守恒断言）**必须对两个引擎都绿**
- 每个 Task 结束时全量 `uv run pytest` 必须绿（当前基线 159 passed）

---

## File Structure

| 文件 | 职责 |
|---|---|
| `src/cn_curriculum_graph/pipeline/edges.py` | 改：剪枝反推 CYCLE |
| `src/cn_curriculum_graph/pipeline/review.py` | 改：抽出共用的边过滤 + 孤儿检测 |
| `src/cn_curriculum_graph/pipeline/run.py` | 改：调用共用函数、传 judge 给 run_all、加 `--engine` |
| `src/cn_curriculum_graph/pipeline/faults.py` | 新：故障注入包裹器与调用计数 |
| `src/cn_curriculum_graph/pipeline/graph.py` | 新：LangGraph A 阶段（六 Node 线性） |
| `src/cn_curriculum_graph/pipeline/graph_fanout.py` | 新：LangGraph B 阶段（Send 扇出） |
| `scripts/compare_orchestration.py` | 新：受控实验，出数据表 |
| `docs/langgraph-vs-handwritten.md` | 新：对比笔记（最终产出） |
| `tests/pipeline/test_*.py` | 各自对应的测试 |

---

### Task 1: 剪枝反推 CYCLE

**Files:**
- Modify: `src/cn_curriculum_graph/pipeline/edges.py`（`candidate_prerequisites`）
- Test: `tests/pipeline/test_edges.py`

**Interfaces:**
- Consumes: `models.TopicDraft`
- Produces: `candidate_prerequisites(drafts) -> dict[str, list[TopicDraft]]`（签名不变，行为收紧）

**背景**：首次真实运行实测，模型对两个**同年级**节点提出了双向边，靠 edge judge 否掉才没成环。剪枝规则只反推了 `GRADE_INVERSION`，没反推 `CYCLE`（`validators/structure.py` 里的 ERROR 码）。设计文档 §3.4 明写剪枝的价值是"生成端不产出校验端注定要拒的东西"。

- [ ] **Step 1: 写失败测试**

在 `tests/pipeline/test_edges.py` 里，把现有的 `test_same_grade_drafts_are_mutual_candidates` **替换**为下面两条（该测试锁定的正是要被改掉的行为）：

```python
def test_same_grade_candidates_are_one_directional():
    """同年级互为候选会让模型产出双向边 —— 真实运行已实测到。
    剪枝直接断掉一个方向：只有 draft_id 字典序在前的才能当后者的前置。"""
    left, right = _draft("a", grade=4), _draft("b", grade=4)

    candidates = candidate_prerequisites([left, right])

    assert [c.draft_id for c in candidates["b"]] == ["a"]
    assert candidates["a"] == []


def test_same_grade_direction_is_stable_regardless_of_input_order():
    """方向由 draft_id 决定，与输入顺序无关 —— 否则同一份数据两次跑出不同的图。"""
    left, right = _draft("a", grade=4), _draft("b", grade=4)

    reversed_order = candidate_prerequisites([right, left])

    assert [c.draft_id for c in reversed_order["b"]] == ["a"]
    assert reversed_order["a"] == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_edges.py -q
```
Expected: FAIL，`assert ['b'] == []`（现在 a 的候选里有 b）

- [ ] **Step 3: 改实现**

把 `candidate_prerequisites` 改成：

```python
def candidate_prerequisites(drafts: list[TopicDraft]) -> dict[str, list[TopicDraft]]:
    """候选前置：年级不晚于目标，且跨度不超过 MAX_GRADE_GAP。

    **同年级只保留单向**（按 draft_id 字典序）。理由：同年级互为候选时，
    模型会对两个节点各点一次头，产出 A→B 且 B→A 的双向边，直接触发校验层的
    CYCLE（ERROR），整批产出被自己的 CI 拒掉。首次真实运行已实测到这一幕
    ——当时是 edge judge 把两条都否了才侥幸没成环，靠判定器兜住而不是靠剪枝挡住。

    按 draft_id 定方向是**取舍不是定论**：简单、确定、零额外调用，
    但方向可能与真实先修关系相反。替代方案是让模型选方向（多一次调用）。
    """
    candidates: dict[str, list[TopicDraft]] = {}
    for target in drafts:
        candidates[target.draft_id] = [
            other
            for other in drafts
            if other.draft_id != target.draft_id
            and other.content.grade_start <= target.content.grade_start
            and target.content.grade_start - other.content.grade_start <= MAX_GRADE_GAP
            # 同年级：只有 draft_id 在前的能当前置，断掉反向候选
            and not (
                other.content.grade_start == target.content.grade_start
                and other.draft_id > target.draft_id
            )
        ]
    return candidates
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_edges.py -q
```
Expected: PASS

- [ ] **Step 5: 跑全量**

```bash
uv run pytest -q
```
Expected: 全绿（若端到端测试因边数变化而失败，说明该测试断言了具体边数——改断言，不要改回实现）

- [ ] **Step 6: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/edges.py tests/pipeline/test_edges.py
git commit -m "fix(edges): 剪枝反推 CYCLE，同年级候选只保留单向

真实运行实测模型会对同年级节点产出双向边，此前靠 edge judge 否掉
才没成环 —— 靠判定器兜住而非靠剪枝挡住。按 draft_id 字典序定方向
是取舍非定论，已在 docstring 说明。"
```

---

### Task 2: 孤儿感知 + CONSISTENCY_SKIPPED 修正 + 抽出共用边过滤

**Files:**
- Modify: `src/cn_curriculum_graph/pipeline/review.py`（新增两个纯函数）
- Modify: `src/cn_curriculum_graph/pipeline/run.py`（改用共用函数、传 judge 给 run_all）
- Test: `tests/pipeline/test_review.py`, `tests/pipeline/test_run.py`

**Interfaces:**
- Consumes: `models.{TopicDraft, ProposedEdge, DropRecord}`、`validators.consistency.Judge`
- Produces:
  - `review.filter_edges_by_kept_drafts(proposed, kept_ids) -> tuple[dict[str, list[ProposedEdge]], list[DropRecord]]`
  - `review.detect_orphans(kept_drafts, proposed_before, kept_edges) -> list[DropRecord]`

**为什么要抽共用函数**：`run_pipeline` 里那段边预过滤 + 丢边记账约 40 行，是**流水线语义**不是编排机制——LangGraph 版必须行为完全一致。复制过去会改一处漏一处。抽成纯函数两边都调，也让 Task 6 的代码行数对比更诚实（量的是编排机制，不是重复的业务逻辑）。

- [ ] **Step 1: 写边过滤的失败测试**

追加到 `tests/pipeline/test_review.py`：

```python
from cn_curriculum_graph.pipeline.review import detect_orphans, filter_edges_by_kept_drafts


def test_filter_edges_records_target_rejected():
    proposed = {"a": [ProposedEdge(prerequisite_draft_id="b", strength="hard", reason="因")]}

    surviving, drops = filter_edges_by_kept_drafts(proposed, kept_ids={"b"})

    assert surviving == {}
    assert drops[0].reason == "EDGE_TARGET_REJECTED"
    assert drops[0].ref == "a<-b"


def test_filter_edges_records_prerequisite_rejected():
    proposed = {"a": [ProposedEdge(prerequisite_draft_id="b", strength="hard", reason="因")]}

    surviving, drops = filter_edges_by_kept_drafts(proposed, kept_ids={"a"})

    assert surviving == {"a": []}
    assert drops[0].reason == "EDGE_PREREQUISITE_REJECTED"
    assert drops[0].ref == "a<-b"


def test_filter_edges_keeps_both_ends_alive():
    proposed = {"a": [ProposedEdge(prerequisite_draft_id="b", strength="hard", reason="因")]}

    surviving, drops = filter_edges_by_kept_drafts(proposed, kept_ids={"a", "b"})

    assert [e.prerequisite_draft_id for e in surviving["a"]] == ["b"]
    assert drops == []
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_review.py -q
```
Expected: FAIL，`ImportError: cannot import name 'filter_edges_by_kept_drafts'`

- [ ] **Step 3: 实现边过滤（把 run.py 里的逻辑原样搬过来）**

追加到 `src/cn_curriculum_graph/pipeline/review.py`：

```python
def filter_edges_by_kept_drafts(
    proposed: dict[str, list[ProposedEdge]], kept_ids: set[str]
) -> tuple[dict[str, list[ProposedEdge]], list[DropRecord]]:
    """审核淘汰节点后，剔除两端不再存活的边，**每条都留痕**。

    这段逻辑原先内联在 run_pipeline 里。抽出来是因为它是流水线语义而非编排
    机制：LangGraph 版必须行为完全一致，复制一份必然改一处漏一处。
    """
    surviving: dict[str, list[ProposedEdge]] = {}
    drops: list[DropRecord] = []

    for target, group in proposed.items():
        if target not in kept_ids:
            drops += [
                DropRecord(
                    stage="review",
                    ref=f"{target}<-{e.prerequisite_draft_id}",
                    reason="EDGE_TARGET_REJECTED",
                    detail=f"目标 draft {target} 被 review 淘汰，指向它的边一并丢弃",
                )
                for e in group
            ]
            continue

        kept: list[ProposedEdge] = []
        for e in group:
            if e.prerequisite_draft_id in kept_ids:
                kept.append(e)
            else:
                drops.append(
                    DropRecord(
                        stage="review",
                        ref=f"{target}<-{e.prerequisite_draft_id}",
                        reason="EDGE_PREREQUISITE_REJECTED",
                        detail=(
                            f"前置 draft {e.prerequisite_draft_id} 被 review 淘汰，"
                            f"边 {target}<-{e.prerequisite_draft_id} 丢弃"
                        ),
                    )
                )
        surviving[target] = kept

    return surviving, drops
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_review.py -q
```
Expected: PASS

- [ ] **Step 5: 写孤儿检测的失败测试**

追加到 `tests/pipeline/test_review.py`：

```python
def test_detect_orphans_flags_a_draft_that_lost_all_prerequisites():
    """基础节点被淘汰后，后继静默失去全部前置 —— 真实运行实测到的语义缺口。"""
    survivor = _draft("child")
    proposed_before = {"child": [ProposedEdge(prerequisite_draft_id="base", strength="hard", reason="因")]}

    drops = detect_orphans([survivor], proposed_before, kept_edges={"child": []})

    assert len(drops) == 1
    assert drops[0].reason == "ORPHANED_BY_REJECTION"
    assert drops[0].ref == "child"
    assert "base" in drops[0].detail


def test_detect_orphans_ignores_drafts_that_never_had_prerequisites():
    """最低年级节点本来就没有前置，不算孤儿 —— 否则每次跑都刷一堆噪声。"""
    survivor = _draft("root")

    assert detect_orphans([survivor], proposed_before={"root": []}, kept_edges={"root": []}) == []


def test_detect_orphans_ignores_drafts_that_kept_at_least_one_prerequisite():
    survivor = _draft("child")
    proposed_before = {
        "child": [
            ProposedEdge(prerequisite_draft_id="gone", strength="hard", reason="因"),
            ProposedEdge(prerequisite_draft_id="alive", strength="soft", reason="因"),
        ]
    }
    kept = {"child": [ProposedEdge(prerequisite_draft_id="alive", strength="soft", reason="因")]}

    assert detect_orphans([survivor], proposed_before, kept_edges=kept) == []
```

- [ ] **Step 6: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_review.py -q
```
Expected: FAIL，`ImportError: cannot import name 'detect_orphans'`

- [ ] **Step 7: 实现孤儿检测**

追加到 `src/cn_curriculum_graph/pipeline/review.py`：

```python
def detect_orphans(
    kept_drafts: list[TopicDraft],
    proposed_before: dict[str, list[ProposedEdge]],
    kept_edges: dict[str, list[ProposedEdge]],
) -> list[DropRecord]:
    """找出"原本有前置、因本次淘汰而失去全部前置"的节点。

    只记账不丢弃：这些节点本身没问题，问题在于它们的前置没了。校验层的
    ISOLATED_TOPIC 只说"这个节点没有边"，说不出"它本来有、是被这次淘汰弄没的"
    —— 后者才是可行动的信息（该去看 dropped.json 里那个被淘汰的前置该不该淘汰）。
    """
    drops: list[DropRecord] = []
    for draft in kept_drafts:
        before = proposed_before.get(draft.draft_id, [])
        after = kept_edges.get(draft.draft_id, [])
        if before and not after:
            lost = ", ".join(e.prerequisite_draft_id for e in before)
            drops.append(
                DropRecord(
                    stage="review",
                    ref=draft.draft_id,
                    reason="ORPHANED_BY_REJECTION",
                    detail=f"原有前置 [{lost}] 全部被淘汰，该节点现已无前置",
                )
            )
    return drops
```

- [ ] **Step 8: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_review.py -q
```
Expected: PASS

- [ ] **Step 9: 写 run_pipeline 接线的失败测试**

追加到 `tests/pipeline/test_run.py`：

```python
def test_pipeline_reports_orphans_created_by_review(tmp_path):
    """端到端：基础节点被淘汰后，后继要被标记为孤儿。"""
    # 用一个让 fidelity judge 只否决 "乙知识点" 的 deps；其余沿用本文件既有 helper
    # （若本文件的 helper 名称不同，按实际名称调整，行为要求不变）
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲条目。\n\n3.1.2 乙条目。\n", encoding="utf-8")
    out = tmp_path / "out"

    run_pipeline(source, out, _deps_rejecting("甲知识点"), model_id="fake", curriculum="c")

    drops = json.loads((out / "dropped.json").read_text(encoding="utf-8"))
    assert any(d["reason"] == "ORPHANED_BY_REJECTION" for d in drops)


def test_pipeline_does_not_claim_consistency_was_skipped(tmp_path):
    """review 层明明跑过 name judge，最终报告却说『已跳过』—— 这条留痕机制
    自己出的岔子，比不留痕更误导人。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲条目。\n", encoding="utf-8")

    findings = run_pipeline(source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c")

    assert not any(f.code == "CONSISTENCY_SKIPPED" for f in findings)
```

其中 `_deps_rejecting(name)` 是本任务新增的 helper，放在 `test_run.py` 已有 fake 构造函数旁边：

```python
def _deps_rejecting(rejected_name: str) -> PipelineDeps:
    """除指定名称外全部通过 fidelity 的 deps，用于制造"基础节点被淘汰"场景。"""
    deps = _fake_deps()
    deps.fidelity_judges = [
        lambda d: Vote(
            reviewer="fake", approved=d.content.name != rejected_name, reason="测试"
        )
    ]
    return deps
```

- [ ] **Step 10: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_run.py -q
```
Expected: FAIL，两条都失败（无 `ORPHANED_BY_REJECTION` 记录；`CONSISTENCY_SKIPPED` 仍在）

- [ ] **Step 11: 改 run_pipeline**

在 `src/cn_curriculum_graph/pipeline/run.py` 的 review 段，把那段内联的边预过滤循环**整段替换**为调用共用函数，并补上孤儿检测与 judge 透传：

```python
    # 5 review
    draft_review = review_mod.review_drafts(
        deduped.kept, deps.fidelity_judges, deps.name_judges
    )
    io.append_drops(drops_path, draft_review.drops)
    kept_ids = {d.draft_id for d in draft_review.kept}

    # 边预过滤：逻辑抽进 review.filter_edges_by_kept_drafts，两个编排实现共用，
    # 避免复制一份后改一处漏一处
    surviving_edges, edge_prefilter_drops = review_mod.filter_edges_by_kept_drafts(
        proposed, kept_ids
    )
    io.append_drops(drops_path, edge_prefilter_drops)

    edge_review = review_mod.review_edges(
        {d.draft_id: d for d in draft_review.kept}, surviving_edges, deps.edge_judges
    )
    io.append_drops(drops_path, edge_review.drops)

    # 淘汰会制造孤儿：原本有前置的节点，前置全被淘汰后就静默失去了依赖。
    # 只记账不丢弃 —— 节点本身没问题，问题在于它的前置没了。
    io.append_drops(
        drops_path,
        review_mod.detect_orphans(draft_review.kept, proposed, edge_review.kept_edges),
    )

    io.write_stage(out_dir / "05-reviewed.json", draft_review.kept)
    io.write_stage(
        out_dir / "review-log.json", draft_review.outcomes + edge_review.outcomes
    )
```

再把 `run_all(graph)` 那行改成把 review 层用过的 name judge 传下去：

```python
    # review 层已经用 name judge 跑过名实一致，把它传给 run_all，
    # 否则最终报告会打印 CONSISTENCY_SKIPPED 说"已跳过"——
    # 这条留痕机制自己出的岔子，比不留痕更误导人。
    findings = run_all(graph, judge=deps.name_judges[0] if deps.name_judges else None)
```

- [ ] **Step 12: 跑测试确认通过**

```bash
uv run pytest -q
```
Expected: 全绿

- [ ] **Step 13: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/review.py src/cn_curriculum_graph/pipeline/run.py tests/pipeline/
git commit -m "fix(pipeline): 孤儿感知 + CONSISTENCY_SKIPPED 误报 + 抽出共用边过滤

首次真实运行暴露的两条：基础节点被淘汰后后继静默变孤儿；review 层
明明跑过 name judge，最终报告却说已跳过。边过滤抽成共用纯函数，
供两个编排实现调用（它是流水线语义不是编排机制）。"
```

---

### Task 3: 故障注入装置

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/faults.py`
- Test: `tests/pipeline/test_faults.py`

**Interfaces:**
- Consumes: `run.PipelineDeps`
- Produces: `FaultSpec`、`CallCounter`、`wrap_deps(deps, specs) -> tuple[PipelineDeps, CallCounter]`

**为什么先做这个**：实验装置不可信，实验数据就不可信。它必须自己有测试。

- [ ] **Step 1: 写失败测试**

创建 `tests/pipeline/test_faults.py`：

```python
"""故障注入装置的自测。装置不可信，实验数据就不可信。

零生产代码改动 —— 因为 PipelineDeps 本来就是依赖注入的。
当初为可测性做的 DI，现在直接变成了实验装置。
"""

import pytest

from cn_curriculum_graph.pipeline.faults import CallCounter, FaultSpec, wrap_deps
from cn_curriculum_graph.pipeline.models import DraftBatch


class _Boom(Exception):
    pass


def _deps_with_counting_extractor():
    """构造一个最小 PipelineDeps，extractor 每次调用返回空 batch。"""
    from cn_curriculum_graph.pipeline.run import PipelineDeps

    return PipelineDeps(
        extractor=lambda chunk: DraftBatch(drafts=[]),
        same_topic_judge=lambda a, b: None,
        edge_proposer=lambda t, c: None,
        fidelity_judges=[lambda d: None],
        name_judges=[lambda name, description: None],
        edge_judges=[lambda t, e: None],
    )


def test_counts_every_call_per_target():
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[])

    deps.extractor("chunk1")
    deps.extractor("chunk2")

    assert counter.counts["extractor"] == 2


def test_raises_on_the_specified_call_only():
    spec = FaultSpec(target="extractor", fail_on_call=2, exc=_Boom, times=1)
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[spec])

    deps.extractor("c1")                       # 第 1 次：正常
    with pytest.raises(_Boom):
        deps.extractor("c2")                   # 第 2 次：炸
    deps.extractor("c3")                       # 第 3 次：恢复正常

    assert counter.counts["extractor"] == 3


def test_times_controls_how_many_consecutive_calls_fail():
    spec = FaultSpec(target="extractor", fail_on_call=1, exc=_Boom, times=2)
    deps, _ = wrap_deps(_deps_with_counting_extractor(), specs=[spec])

    with pytest.raises(_Boom):
        deps.extractor("c1")
    with pytest.raises(_Boom):
        deps.extractor("c2")
    deps.extractor("c3")   # 第 3 次不再炸


def test_wraps_judges_inside_lists():
    """judges 是列表，包裹器要能钻进列表里逐个包。"""
    spec = FaultSpec(target="fidelity_judges", fail_on_call=1, exc=_Boom, times=1)
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[spec])

    with pytest.raises(_Boom):
        deps.fidelity_judges[0]("draft")

    assert counter.counts["fidelity_judges"] == 1


def test_counter_is_reset_between_runs():
    deps, counter = wrap_deps(_deps_with_counting_extractor(), specs=[])
    deps.extractor("c1")

    counter.reset()

    assert counter.counts == {}


def test_unknown_target_is_rejected_loudly():
    """写错 target 名字就该当场炸 —— 否则实验会静默地什么都没注入。"""
    with pytest.raises(ValueError, match="没有这个依赖项"):
        wrap_deps(_deps_with_counting_extractor(), specs=[FaultSpec(target="typo", fail_on_call=1, exc=_Boom)])
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_faults.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.faults'`

- [ ] **Step 3: 实现**

创建 `src/cn_curriculum_graph/pipeline/faults.py`：

```python
"""受控实验的故障注入装置。

把 PipelineDeps 里每个可调用项换成「计数 + 可控失败」的代理，
**不改任何生产代码** —— 因为 deps 本来就是依赖注入的。

这本身是对比笔记的一条素材：当初为可测性做的 DI，
现在原封不动变成了实验装置。
"""

from __future__ import annotations

import dataclasses
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any


@dataclass
class FaultSpec:
    """在第 fail_on_call 次调用 target 时开始抛 exc，连抛 times 次。"""

    target: str
    fail_on_call: int
    exc: type[Exception]
    times: int = 1


@dataclass
class CallCounter:
    counts: dict[str, int] = field(default_factory=lambda: defaultdict(int))

    def reset(self) -> None:
        self.counts = defaultdict(int)


def _proxy(fn: Any, name: str, counter: CallCounter, specs: list[FaultSpec]) -> Any:
    mine = [s for s in specs if s.target == name]

    def wrapped(*args: Any, **kwargs: Any) -> Any:
        counter.counts[name] += 1
        n = counter.counts[name]
        for spec in mine:
            if spec.fail_on_call <= n < spec.fail_on_call + spec.times:
                raise spec.exc(f"注入故障：{name} 第 {n} 次调用")
        return fn(*args, **kwargs)

    return wrapped


def wrap_deps(deps: Any, specs: list[FaultSpec]) -> tuple[Any, CallCounter]:
    """返回一份包裹过的 deps 副本与计数器。原 deps 不被修改。"""
    counter = CallCounter()
    field_names = {f.name for f in dataclasses.fields(deps)}

    for spec in specs:
        if spec.target not in field_names:
            raise ValueError(f"没有这个依赖项：{spec.target}（可用：{sorted(field_names)}）")

    changes: dict[str, Any] = {}
    for name in field_names:
        value = getattr(deps, name)
        if isinstance(value, list):
            changes[name] = [_proxy(v, name, counter, specs) for v in value]
        else:
            changes[name] = _proxy(value, name, counter, specs)

    return dataclasses.replace(deps, **changes), counter
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_faults.py -q
```
Expected: PASS，6 passed

- [ ] **Step 5: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/faults.py tests/pipeline/test_faults.py
git commit -m "feat(pipeline): 故障注入装置，零生产代码改动

包裹 PipelineDeps 的可调用项做计数与可控失败。装置自身有测试 ——
装置不可信，实验数据就不可信。"
```

---

### Task 4: LangGraph A 阶段（六 Node 线性）

**Files:**
- Modify: `pyproject.toml`（加依赖）
- Create: `src/cn_curriculum_graph/pipeline/graph.py`
- Test: `tests/pipeline/test_graph.py`

**Interfaces:**
- Consumes: 六层已有函数、`run.PipelineDeps`、`review.{filter_edges_by_kept_drafts, detect_orphans}`、`assemble.dedupe_edges_by_pair`
- Produces: `PipelineState`（TypedDict）、`build_graph() -> StateGraph`、`run_pipeline_lg(source_dir, out_dir, deps, model_id, curriculum, checkpoint_db=None, thread_id="default") -> list[Finding]`

**关键约束**：Node 里**只做**「取 state → 调已有函数 → 返回 delta」。写业务逻辑就把对比毁了。

- [ ] **Step 1: 加依赖**

```bash
uv add langgraph langgraph-checkpoint-sqlite
uv run python -c "import langgraph, importlib.metadata as m; print(m.version('langgraph'))"
```
Expected: 打印 `1.2.9` 或更高

- [ ] **Step 2: 写失败测试**

创建 `tests/pipeline/test_graph.py`：

```python
"""LangGraph 编排的对等性测试。

硬标准：与手写版行为一致。所以这里只测"结构上是不是六个 Node、
state 累加语义对不对"，行为一致性由 test_run.py 的参数化端到端测试保证。
"""

import operator
from typing import Annotated, get_type_hints

from cn_curriculum_graph.pipeline.graph import PipelineState, build_graph


def test_state_accumulates_drops_across_nodes():
    """drops 是唯一带 reducer 的字段 —— 手写版五处显式 append_drops，
    这里声明一次，累加语义成了类型的一部分。"""
    hints = get_type_hints(PipelineState, include_extras=True)
    assert hints["drops"].__metadata__ == (operator.add,)


def test_other_fields_are_overwrite_not_accumulate():
    """除 drops 外都是覆盖语义，别不小心给 chunks 也加了 reducer。"""
    hints = get_type_hints(PipelineState, include_extras=True)
    assert not hasattr(hints["chunks"], "__metadata__")


def test_graph_has_one_node_per_pipeline_layer():
    """六层一一对应。多一个少一个都说明 Node 里塞了不该塞的东西。"""
    compiled = build_graph().compile()
    nodes = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

    assert nodes == {"chunk", "extract", "dedupe", "edges", "review", "assemble"}


def test_graph_is_linear():
    compiled = build_graph().compile()
    edges = {(e.source, e.target) for e in compiled.get_graph().edges}

    assert ("chunk", "extract") in edges
    assert ("extract", "dedupe") in edges
    assert ("dedupe", "edges") in edges
    assert ("edges", "review") in edges
    assert ("review", "assemble") in edges
```

- [ ] **Step 3: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_graph.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'cn_curriculum_graph.pipeline.graph'`

- [ ] **Step 4: 实现**

创建 `src/cn_curriculum_graph/pipeline/graph.py`：

```python
"""LangGraph 版编排（A 阶段：一层一个 Node，与手写版对等）。

与 run.py 的关系：**共用同一套六层纯函数**，只有编排方式不同。
这是路线图阶段四「同一需求分别用手写和框架实现」的框架半边。

Node 里只做「取 state → 调那层已有函数 → 返回 delta」，
不写任何业务逻辑 —— 否则对比就变成"手写版 vs 框架版+重构"，不公平。

已核实 API（langgraph 1.2.9，2026-07-26 实测）：节点签名 (state, runtime)，
运行时依赖走 runtime.context，不进 checkpoint。
"""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph

from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import chunk as chunk_mod
from cn_curriculum_graph.pipeline import dedupe as dedupe_mod
from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.models import (
    Chunk,
    DropRecord,
    Merge,
    ProposedEdge,
    ReviewOutcome,
    TargetedEdge,
    TopicDraft,
)
from cn_curriculum_graph.pipeline.run import DEFAULT_CURRICULUM, PipelineDeps
from cn_curriculum_graph.runner import run_all
from cn_curriculum_graph.validators.base import Finding, Severity


class PipelineState(TypedDict, total=False):
    """drops 是唯一带 reducer 的字段：手写版有五处显式 io.append_drops，
    这里声明一次，累加语义成了类型的一部分。

    代价是丢了"每层跑完立刻落盘"的时序保证 —— 所以每个 Node 仍显式调
    io.write_stage，中间产物可人眼检查是项目原则，不是手写版的实现细节。
    """

    source_dir: str
    out_dir: str
    model_id: str
    curriculum: str

    chunks: list[Chunk]
    drafts: list[TopicDraft]
    deduped: list[TopicDraft]
    merges: list[Merge]
    proposed: dict[str, list[ProposedEdge]]
    reviewed: list[TopicDraft]
    kept_edges: dict[str, list[ProposedEdge]]
    outcomes: list[ReviewOutcome]
    findings: list[Finding]

    drops: Annotated[list[DropRecord], operator.add]


def _out(state: PipelineState) -> Path:
    return Path(state["out_dir"])


def node_chunk(state: PipelineState, runtime) -> dict:
    chunks: list[Chunk] = []
    drops: list[DropRecord] = []
    for path in sorted(Path(state["source_dir"]).glob("*.md")):
        produced, dropped = chunk_mod.split_source(
            path.read_text(encoding="utf-8"), source_file=path.name
        )
        chunks += produced
        drops += dropped
    io.write_stage(_out(state) / "01-chunks.json", chunks)
    return {"chunks": chunks, "drops": drops}


def node_extract(state: PipelineState, runtime) -> dict:
    drafts, drops = extract_mod.extract_all(state["chunks"], runtime.context.extractor)
    io.write_stage(_out(state) / "02-drafts.json", drafts)
    return {"drafts": drafts, "drops": drops}


def node_dedupe(state: PipelineState, runtime) -> dict:
    result = dedupe_mod.dedupe(state["drafts"], runtime.context.same_topic_judge)
    io.write_stage(_out(state) / "03-deduped.json", result.kept)
    io.write_stage(_out(state) / "merges.json", result.merges)
    return {"deduped": result.kept, "merges": result.merges, "drops": result.drops}


def node_edges(state: PipelineState, runtime) -> dict:
    proposed, drops = edges_mod.propose_all(state["deduped"], runtime.context.edge_proposer)
    io.write_stage(
        _out(state) / "04-edges.json",
        [
            TargetedEdge(target_draft_id=target, edge=e)
            for target, group in proposed.items()
            for e in group
        ],
    )
    return {"proposed": proposed, "drops": drops}


def node_review(state: PipelineState, runtime) -> dict:
    deps = runtime.context
    draft_review = review_mod.review_drafts(
        state["deduped"], deps.fidelity_judges, deps.name_judges
    )
    kept_ids = {d.draft_id for d in draft_review.kept}
    surviving, prefilter_drops = review_mod.filter_edges_by_kept_drafts(
        state["proposed"], kept_ids
    )
    edge_review = review_mod.review_edges(
        {d.draft_id: d for d in draft_review.kept}, surviving, deps.edge_judges
    )
    orphan_drops = review_mod.detect_orphans(
        draft_review.kept, state["proposed"], edge_review.kept_edges
    )
    io.write_stage(_out(state) / "05-reviewed.json", draft_review.kept)
    io.write_stage(
        _out(state) / "review-log.json", draft_review.outcomes + edge_review.outcomes
    )
    return {
        "reviewed": draft_review.kept,
        "kept_edges": edge_review.kept_edges,
        "outcomes": draft_review.outcomes + edge_review.outcomes,
        "drops": draft_review.drops + prefilter_drops + edge_review.drops + orphan_drops,
    }


def node_assemble(state: PipelineState, runtime) -> dict:
    deduped_edges, dup_drops = assemble_mod.dedupe_edges_by_pair(state["kept_edges"])
    graph = assemble_mod.assemble(
        state["reviewed"],
        deduped_edges,
        model_id=state["model_id"],
        curriculum=state["curriculum"],
    )
    (_out(state) / "graph.json").write_text(
        graph.model_dump_json(indent=2, exclude_none=False) + "\n", encoding="utf-8"
    )
    name_judges = runtime.context.name_judges
    findings = run_all(graph, judge=name_judges[0] if name_judges else None)
    if not graph.topics:
        findings.append(
            Finding(
                code="EMPTY_GENERATION",
                severity=Severity.ERROR,
                message=(
                    "本次生成没有产出任何知识点节点 —— 请查看 "
                    f"{_out(state) / 'dropped.json'} 定位是哪一层把输入全部丢弃了"
                ),
                context={
                    "chunks": len(state["chunks"]),
                    "drafts": len(state["drafts"]),
                    "deduped": len(state["deduped"]),
                    "reviewed": len(state["reviewed"]),
                },
            )
        )
    return {"findings": findings, "drops": dup_drops}


def build_graph() -> StateGraph:
    g = StateGraph(PipelineState, context_schema=PipelineDeps)
    g.add_node("chunk", node_chunk)
    g.add_node("extract", node_extract)
    g.add_node("dedupe", node_dedupe)
    g.add_node("edges", node_edges)
    g.add_node("review", node_review)
    g.add_node("assemble", node_assemble)
    g.add_edge(START, "chunk")
    g.add_edge("chunk", "extract")
    g.add_edge("extract", "dedupe")
    g.add_edge("dedupe", "edges")
    g.add_edge("edges", "review")
    g.add_edge("review", "assemble")
    g.add_edge("assemble", END)
    return g


def run_pipeline_lg(
    source_dir: Path,
    out_dir: Path,
    deps: PipelineDeps,
    model_id: str,
    curriculum: str = DEFAULT_CURRICULUM,
) -> list[Finding]:
    """与 run.run_pipeline 行为对等的 LangGraph 版入口（不带 checkpointer）。

    checkpointer 与容错策略在 Task 5 加上 —— 先证明对等，再谈框架红利。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    app = build_graph().compile()
    result = app.invoke(
        {
            "source_dir": str(source_dir),
            "out_dir": str(out_dir),
            "model_id": model_id,
            "curriculum": curriculum,
            "drops": [],
        },
        context=deps,
    )
    io.append_drops(out_dir / "dropped.json", result["drops"])
    return result["findings"]
```

- [ ] **Step 5: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_graph.py -q
```
Expected: PASS，4 passed

- [ ] **Step 6: 把端到端测试参数化到两个引擎**

修改 `tests/pipeline/test_run.py`：在文件顶部加一个引擎分发 helper，并给所有调用 `run_pipeline(...)` 的端到端测试加 `@pytest.mark.parametrize("engine", ["handwritten", "langgraph"])`，把直接调用改成走 helper：

```python
import pytest

from cn_curriculum_graph.pipeline.graph import run_pipeline_lg


def _run(engine: str, source_dir, out_dir, deps, model_id, curriculum):
    """两个编排实现的统一入口。测试对实现无感知，才谈得上『对等』。"""
    if engine == "handwritten":
        return run_pipeline(source_dir, out_dir, deps, model_id=model_id, curriculum=curriculum)
    if engine == "langgraph":
        return run_pipeline_lg(source_dir, out_dir, deps, model_id=model_id, curriculum=curriculum)
    raise ValueError(f"未知引擎：{engine}")
```

**逐个改造本文件里已有的端到端测试**（含跨层守恒断言那条），把
`run_pipeline(src, out, deps, model_id=..., curriculum=...)` 换成
`_run(engine, src, out, deps, ..., ...)`，并给函数签名加 `engine` 参数与 parametrize 装饰器。
纯单元测试（不跑整条管道的）不需要参数化。

- [ ] **Step 7: 跑全量确认两个引擎都绿**

```bash
uv run pytest -q
```
Expected: 全绿，端到端用例数翻倍

若 LangGraph 版有测试失败，**改 graph.py 让它符合手写版行为，不要改测试放宽标准**——测试对两个实现一视同仁是本任务的定义。

- [ ] **Step 8: 提交**

```bash
git add pyproject.toml uv.lock src/cn_curriculum_graph/pipeline/graph.py tests/pipeline/
git commit -m "feat(pipeline): LangGraph 编排 A 阶段，与手写版对等

六层纯函数一行不改，Node 只做取 state → 调已有函数 → 返回 delta。
drops 用 Annotated reducer 替代五处显式 append_drops。
端到端测试参数化到两个引擎，同一套断言都必须绿。"
```

---

### Task 5: 容错三件套 + checkpointer 重入 + CLI 开关

**Files:**
- Modify: `src/cn_curriculum_graph/pipeline/graph.py`
- Modify: `src/cn_curriculum_graph/pipeline/run.py`（`main` 加 `--engine`）
- Modify: `pyproject.toml`
- Test: `tests/pipeline/test_graph.py`

**Interfaces:**
- Consumes: Task 3 的 `wrap_deps`、`FaultSpec`
- Produces: `run_pipeline_lg(..., checkpoint_db: Path | None = None, thread_id: str = "default")`、`PROGRAMMING_ERRORS`、`retry_on`

- [ ] **Step 1: 写失败测试**

追加到 `tests/pipeline/test_graph.py`：

```python
import pytest

from cn_curriculum_graph.pipeline.faults import FaultSpec, wrap_deps
from cn_curriculum_graph.pipeline.graph import retry_on


class _Rate(Exception):
    pass


def test_retry_on_skips_programming_errors():
    """程序 bug 不该被重试 —— 重试三次只会把同一个 bug 犯三遍。
    与手写版收窄 except 的策略同源。"""
    assert retry_on(_Rate("429")) is True
    for exc in (AttributeError(), TypeError(), NameError(), KeyError()):
        assert retry_on(exc) is False


def test_transient_failure_is_retried_and_recovers(tmp_path):
    """第 2 次 extract 调用抛一次瞬时错误，重试应当自愈，管道跑完。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n\n3.1.2 乙。\n", encoding="utf-8")
    deps, counter = wrap_deps(
        _fake_deps(), [FaultSpec(target="extractor", fail_on_call=2, exc=_Rate, times=1)]
    )

    findings = run_pipeline_lg(source, tmp_path / "out", deps, model_id="fake", curriculum="c")

    assert (tmp_path / "out" / "graph.json").exists()
    # 第 2 次失败后被重试，所以 extractor 总调用次数比 chunk 数多
    assert counter.counts["extractor"] == 3


def test_checkpoint_resumes_instead_of_rerunning_completed_nodes(tmp_path):
    """不可恢复的失败 + checkpointer：第二次跑不应重跑已完成的 chunk/extract。"""
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n\n3.1.2 乙。\n", encoding="utf-8")
    db = tmp_path / "cp.sqlite"

    # 第一次：edges 层必炸（times 大到重试也救不回来）
    boom_deps, boom_counter = wrap_deps(
        _fake_deps(), [FaultSpec(target="edge_proposer", fail_on_call=1, exc=_Rate, times=99)]
    )
    with pytest.raises(Exception):
        run_pipeline_lg(source, tmp_path / "out", boom_deps, model_id="fake",
                        curriculum="c", checkpoint_db=db, thread_id="t1")
    first_extract_calls = boom_counter.counts["extractor"]
    assert first_extract_calls > 0

    # 第二次：同一 thread_id 续跑，extract 不该被重新调用
    good_deps, good_counter = wrap_deps(_fake_deps(), [])
    run_pipeline_lg(source, tmp_path / "out", good_deps, model_id="fake",
                    curriculum="c", checkpoint_db=db, thread_id="t1")

    assert good_counter.counts["extractor"] == 0
    assert (tmp_path / "out" / "graph.json").exists()
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_graph.py -q
```
Expected: FAIL，`ImportError: cannot import name 'retry_on'`

- [ ] **Step 3: 实现**

在 `src/cn_curriculum_graph/pipeline/graph.py` 顶部加：

```python
from datetime import timedelta

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import RetryPolicy

# 程序 bug 不该被重试 —— 重试三次只会把同一个 bug 犯三遍。
# 与手写版收窄 except 的策略同源（见 extract.py 的 _PROGRAMMING_ERRORS）。
PROGRAMMING_ERRORS = (AttributeError, TypeError, NameError, KeyError)


def retry_on(exc: Exception) -> bool:
    return not isinstance(exc, PROGRAMMING_ERRORS)


RETRY_POLICY = RetryPolicy(max_attempts=3, retry_on=retry_on)
# 单个 LLM 层最长容忍时间。手写版**根本没有超时概念** —— 这一条是框架白送的。
# 公开命名（无下划线前缀）：graph_fanout.py 要跨模块复用它们。
NODE_TIMEOUT = timedelta(minutes=10)
```

把 `build_graph()` 里三个走 LLM 的 Node 挂上策略（纯规则的 chunk / assemble 不需要）：

```python
    g.add_node("extract", node_extract, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("dedupe", node_dedupe, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("edges", node_edges, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("review", node_review, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
```

把 `run_pipeline_lg` 改成支持 checkpointer：

```python
def run_pipeline_lg(
    source_dir: Path,
    out_dir: Path,
    deps: PipelineDeps,
    model_id: str,
    curriculum: str = DEFAULT_CURRICULUM,
    checkpoint_db: Path | None = None,
    thread_id: str = "default",
) -> list[Finding]:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_dir": str(source_dir),
        "out_dir": str(out_dir),
        "model_id": model_id,
        "curriculum": curriculum,
        "drops": [],
    }
    config = {"configurable": {"thread_id": thread_id}}

    if checkpoint_db is None:
        result = build_graph().compile().invoke(payload, config=config, context=deps)
    else:
        with SqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
            app = build_graph().compile(checkpointer=saver)
            # 续跑：同一 thread_id 已有 checkpoint 时传 None，
            # LangGraph 会从上次中断处继续，而不是从头再来
            existing = app.get_state(config)
            resume = existing.next != ()
            result = app.invoke(None if resume else payload, config=config, context=deps)

    io.append_drops(out_dir / "dropped.json", result["drops"])
    return result["findings"]
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_graph.py -q
```
Expected: PASS

- [ ] **Step 5: 加 CLI 开关**

在 `src/cn_curriculum_graph/pipeline/run.py` 的 `main()` 里加参数并分发：

```python
    parser.add_argument(
        "--engine",
        choices=("handwritten", "langgraph"),
        default="handwritten",
        help="编排实现。两者行为对等，langgraph 版额外支持 --checkpoint 断点续跑",
    )
    parser.add_argument(
        "--checkpoint", type=Path, default=None, help="checkpoint 数据库路径（仅 langgraph）"
    )
```

在调用处分发：

```python
    if args.engine == "langgraph":
        from cn_curriculum_graph.pipeline.graph import run_pipeline_lg

        findings = run_pipeline_lg(
            args.source, args.out, build_deepseek_deps(models),
            model_id=models[0], curriculum=args.curriculum,
            checkpoint_db=args.checkpoint,
        )
    else:
        if args.checkpoint is not None:
            parser.error("--checkpoint 仅 --engine langgraph 支持（手写版没有重入能力，这正是对比要量的东西）")
        findings = run_pipeline(
            args.source, args.out, build_deepseek_deps(models),
            model_id=models[0], curriculum=args.curriculum,
        )
```

- [ ] **Step 6: 跑全量并验 CLI**

```bash
uv run pytest -q
uv run ccg-generate --help
```
Expected: 测试全绿；help 里出现 `--engine` 与 `--checkpoint`

- [ ] **Step 7: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/graph.py src/cn_curriculum_graph/pipeline/run.py tests/pipeline/test_graph.py
git commit -m "feat(pipeline): LangGraph 版容错三件套 + checkpointer 重入 + --engine 开关

retry_policy 排除程序 bug（与手写版收窄 except 同源）、timeout（手写版
根本没有这个概念）、SqliteSaver 断点续跑。手写版传 --checkpoint 直接
报错说明它没有重入能力 —— 这正是对比要量的东西。"
```

---

### Task 6: 受控实验脚本 + 第一章数据

**Files:**
- Create: `scripts/compare_orchestration.py`
- Test: `tests/pipeline/test_compare_script.py`

**Interfaces:**
- Consumes: `run.run_pipeline`、`graph.run_pipeline_lg`、`faults.{FaultSpec, wrap_deps}`
- Produces: `SCENARIOS`、`run_scenario(engine, scenario, tmp_root) -> ScenarioResult`、`main()`

- [ ] **Step 1: 写失败测试**

创建 `tests/pipeline/test_compare_script.py`：

```python
"""实验脚本的自测：度量口径必须可信，否则笔记里的数字是假的。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))

from compare_orchestration import SCENARIOS, ScenarioResult, run_scenario


def test_scenarios_cover_the_three_designed_faults():
    assert {s.name for s in SCENARIOS} == {"baseline", "rate_limit", "hard_crash"}


def test_baseline_completes_on_both_engines(tmp_path):
    for engine in ("handwritten", "langgraph"):
        result = run_scenario(engine, SCENARIOS[0], tmp_path / engine)
        assert isinstance(result, ScenarioResult)
        assert result.completed is True
        assert result.total_calls > 0


def test_hard_crash_shows_rerun_cost_difference(tmp_path):
    """手写版没有重入能力，崩溃后第二次要从头跑；LangGraph 版从断点续。
    这个差值就是笔记第一章的核心数字。"""
    hw = run_scenario("handwritten", SCENARIOS[2], tmp_path / "hw")
    lg = run_scenario("langgraph", SCENARIOS[2], tmp_path / "lg")

    assert hw.recovery_calls > lg.recovery_calls
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_compare_script.py -q
```
Expected: FAIL，`ModuleNotFoundError: No module named 'compare_orchestration'`

- [ ] **Step 3: 实现**

创建 `scripts/compare_orchestration.py`：

```python
"""受控实验：同一份素材、同样的故障，量两个编排实现的差异。

用 fake 而非真模型：可复现、免费、故障位置精确可控。
代价是测不出真实限流的行为（真实 429 常伴随 Retry-After 头、并发退避），
这条限制必须写进笔记，不能让读者以为是生产环境实测。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from cn_curriculum_graph.pipeline.faults import FaultSpec, wrap_deps
from cn_curriculum_graph.pipeline.graph import run_pipeline_lg
from cn_curriculum_graph.pipeline.run import run_pipeline

SOURCE = """3.1.1 能认识、读、写万以内的数。

3.1.2 能理解小数的意义。

3.1.3 能理解分数的意义。
"""


class RateLimit(Exception):
    """模拟 429。"""


class HardCrash(Exception):
    """模拟不可恢复的中途崩溃。"""


@dataclass(frozen=True)
class Scenario:
    name: str
    specs: tuple[FaultSpec, ...]
    note: str


SCENARIOS = [
    Scenario("baseline", (), "无故障，量正常路径的调用次数与耗时"),
    Scenario(
        "rate_limit",
        (FaultSpec(target="fidelity_judges", fail_on_call=2, exc=RateLimit, times=1),),
        "第 2 次 fidelity 判定抛 429，考察重试能否自愈",
    ),
    Scenario(
        "hard_crash",
        (FaultSpec(target="edge_proposer", fail_on_call=1, exc=HardCrash, times=99),),
        "edges 层不可恢复地崩溃，考察恢复时从哪里续跑",
    ),
]


@dataclass
class ScenarioResult:
    engine: str
    scenario: str
    completed: bool
    total_calls: int
    recovery_calls: int
    seconds: float


def _fake_deps():
    """全 fake 的 deps。实现细节参照 tests/pipeline/test_run.py 里的同名构造，
    此处独立一份，避免脚本依赖测试代码。"""
    from cn_curriculum_graph.pipeline.dedupe import SameTopicVerdict
    from cn_curriculum_graph.pipeline.models import (
        DraftBatch,
        DraftContent,
        ProposedEdge,
        ProposedEdgeBatch,
        Vote,
    )
    from cn_curriculum_graph.pipeline.run import PipelineDeps
    from cn_curriculum_graph.validators.consistency import Verdict

    names = {"3.1.1": ("万以内数的认识", 2), "3.1.2": ("小数的意义", 3), "3.1.3": ("分数的意义", 3)}

    def extractor(chunk):
        name, grade = names[chunk.standard_code]
        return DraftBatch(
            drafts=[
                DraftContent(
                    name=name, description=f"{name}的内容", type="conceptual",
                    subject="数学", domain="数与代数", grade_start=grade, grade_end=grade,
                    evidence=[f"能演示{name}"], assessment_prompt=f"说说{name}?",
                    source_span=chunk.text,
                )
            ]
        )

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[
                ProposedEdge(prerequisite_draft_id=c.draft_id, strength="hard", reason="先学它")
                for c in candidates
            ]
        )

    return PipelineDeps(
        extractor=extractor,
        same_topic_judge=lambda a, b: SameTopicVerdict(same=False, reason="不同"),
        edge_proposer=proposer,
        fidelity_judges=[lambda d: Vote(reviewer="fake", approved=True, reason="ok")],
        name_judges=[lambda name, description: Verdict(judgment="consistent")],
        edge_judges=[lambda t, e: Vote(reviewer="fake", approved=True, reason="ok")],
    )


def run_scenario(engine: str, scenario: Scenario, root: Path) -> ScenarioResult:
    """跑一个场景。有故障时跑两轮（第一轮撞故障，第二轮恢复），
    recovery_calls 记的是第二轮的调用次数 —— 那就是"重复烧掉的量"。"""
    if root.exists():
        shutil.rmtree(root)
    source = root / "source"
    source.mkdir(parents=True)
    (source / "m.md").write_text(SOURCE, encoding="utf-8")
    out = root / "out"
    db = root / "cp.sqlite"

    started = time.monotonic()
    first_deps, first_counter = wrap_deps(_fake_deps(), list(scenario.specs))
    completed = True
    try:
        _invoke(engine, source, out, first_deps, db)
    except Exception:
        completed = False
    first_calls = sum(first_counter.counts.values())

    recovery_calls = 0
    if not completed:
        # 第二轮：故障已消失，看各自要重跑多少
        second_deps, second_counter = wrap_deps(_fake_deps(), [])
        _invoke(engine, source, out, second_deps, db)
        recovery_calls = sum(second_counter.counts.values())
        completed = True

    return ScenarioResult(
        engine=engine,
        scenario=scenario.name,
        completed=completed,
        total_calls=first_calls + recovery_calls,
        recovery_calls=recovery_calls,
        seconds=round(time.monotonic() - started, 3),
    )


def _invoke(engine: str, source: Path, out: Path, deps, db: Path) -> None:
    if engine == "handwritten":
        # 手写版没有重入能力：第二轮必然从头跑，这正是要量的差异
        run_pipeline(source, out, deps, model_id="fake", curriculum="c")
    else:
        run_pipeline_lg(
            source, out, deps, model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="exp",
        )


def main() -> int:
    root = Path("/tmp/ccg-compare")
    rows: list[ScenarioResult] = []
    for scenario in SCENARIOS:
        for engine in ("handwritten", "langgraph"):
            rows.append(run_scenario(engine, scenario, root / f"{scenario.name}-{engine}"))

    print(f"{'场景':<14}{'引擎':<14}{'完成':<6}{'总调用':<8}{'恢复重跑':<10}耗时(s)")
    print("-" * 64)
    for r in rows:
        print(
            f"{r.scenario:<14}{r.engine:<14}{'是' if r.completed else '否':<6}"
            f"{r.total_calls:<8}{r.recovery_calls:<10}{r.seconds}"
        )
    print("\n注：fake 实现，非真实 API；恢复重跑 = 故障消失后第二轮的调用次数。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_compare_script.py -q
```
Expected: PASS

- [ ] **Step 5: 跑实验，把数据存下来**

```bash
uv run python scripts/compare_orchestration.py | tee /tmp/compare-a.txt
```
Expected: 打印六行数据（3 场景 × 2 引擎）。**把输出原样保存**，Task 8 写笔记要用。

- [ ] **Step 6: 数编排层代码行数（口径见设计文档 §5）**

```bash
# 手写版：run_pipeline 函数体
awk '/^def run_pipeline\(/,/^def main\(/' src/cn_curriculum_graph/pipeline/run.py | grep -vc '^\s*$\|^\s*#'
# LangGraph 版：PipelineState + 六个 node_* + build_graph + run_pipeline_lg
grep -vc '^\s*$\|^\s*#' src/cn_curriculum_graph/pipeline/graph.py
```
把两个数字记下来（注意 LangGraph 版那条会多算 import 与 docstring，实际写笔记时按设计文档 §5 的口径手工核减，并在笔记里说明核减方式）。

- [ ] **Step 7: 提交**

```bash
git add scripts/compare_orchestration.py tests/pipeline/test_compare_script.py
git commit -m "feat(scripts): 受控实验脚本，量两个编排实现的故障恢复差异

三场景 × 两引擎：baseline / 429 限流 / 中途崩溃。度量总调用次数、
恢复重跑次数、耗时。脚本自身有测试 —— 度量口径不可信，笔记里的数字就是假的。"
```

---

### Task 7: B 阶段 —— `Send` 扇出

**Files:**
- Create: `src/cn_curriculum_graph/pipeline/graph_fanout.py`
- Test: `tests/pipeline/test_graph_fanout.py`

**Interfaces:**
- Consumes: Task 4 的 `PipelineState` 与各 node 函数
- Produces: `build_fanout_graph() -> StateGraph`、`run_pipeline_fanout(...) -> list[Finding]`

**只把 extract 与 review 扇出**：dedupe 与 edges 需要全局视野（要看到全部 draft 才能配对、才能算候选前置），扇不开。

- [ ] **Step 1: 写失败测试**

创建 `tests/pipeline/test_graph_fanout.py`：

```python
"""B 阶段：条目级扇出。

⚠️ 这已不是"同一需求的两种实现" —— 架构变了。它回答的是另一个问题
（框架解锁了什么），笔记里必须与第一章分开读。
"""

from cn_curriculum_graph.pipeline.graph_fanout import build_fanout_graph


def test_fanout_graph_has_a_per_chunk_extract_node():
    compiled = build_fanout_graph().compile()
    nodes = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

    assert "extract_one" in nodes
    assert "review_one" in nodes


def test_dedupe_and_edges_stay_whole_batch():
    """这两层需要全局视野（配对要看全部 draft、候选前置要看全部节点），扇不开。"""
    compiled = build_fanout_graph().compile()
    nodes = set(compiled.get_graph().nodes) - {"__start__", "__end__"}

    assert "dedupe" in nodes and "dedupe_one" not in nodes
    assert "edges" in nodes and "edges_one" not in nodes
```

- [ ] **Step 2: 跑测试确认失败**

```bash
uv run pytest tests/pipeline/test_graph_fanout.py -q
```
Expected: FAIL，`ModuleNotFoundError`

- [ ] **Step 3: 实现**

创建 `src/cn_curriculum_graph/pipeline/graph_fanout.py`：

```python
"""LangGraph 版 B 阶段：把 extract 与 review 扇出到条目级。

**这已不是与手写版对等的实现** —— 架构变了。它回答的是另一个问题：
框架让我做成了原本不会去写的事吗？收益多大？

关键差异：A 阶段 checkpoint 粒度是"层"，429 恢复后重跑整层（含已成功的
条目）；B 阶段粒度是"条目"，只重跑失败那一条。这个差值就是第二章的数字。

dedupe 与 edges 不扇出：它们需要全局视野（配对要看到全部 draft、
候选前置要看到全部节点），扇开就不对了。
"""

from __future__ import annotations

import operator
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Send

from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.graph import (
    NODE_TIMEOUT,
    RETRY_POLICY,
    PipelineState,
    node_assemble,
    node_chunk,
    node_dedupe,
    node_edges,
)
from cn_curriculum_graph.pipeline.models import Chunk, DropRecord, TopicDraft


class _ExtractOne(TypedDict):
    """单条 chunk 的扇出输入。"""

    chunk: Chunk
    out_dir: str


def fan_out_chunks(state: PipelineState) -> list[Send]:
    return [
        Send("extract_one", {"chunk": c, "out_dir": state["out_dir"]})
        for c in state["chunks"]
    ]


def node_extract_one(payload: _ExtractOne, runtime) -> dict:
    """一个 chunk 一次调用。失败只影响这一条 —— 这就是条目级 checkpoint 的价值。"""
    drafts, drops = extract_mod.extract_all([payload["chunk"]], runtime.context.extractor)
    return {"drafts": drafts, "drops": drops}


def build_fanout_graph() -> StateGraph:
    g = StateGraph(PipelineState, context_schema=type(None))
    g.add_node("chunk", node_chunk)
    g.add_node("extract_one", node_extract_one, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("collect_extract", _collect_extract)
    g.add_node("dedupe", node_dedupe, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("edges", node_edges, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("review_one", _review_one, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("review", _review_collect)
    g.add_node("assemble", node_assemble)

    g.add_edge(START, "chunk")
    g.add_conditional_edges("chunk", fan_out_chunks, ["extract_one"])
    g.add_edge("extract_one", "collect_extract")
    g.add_edge("collect_extract", "dedupe")
    g.add_edge("dedupe", "edges")
    g.add_edge("edges", "review")
    g.add_edge("review", "assemble")
    g.add_edge("assemble", END)
    return g
```

**实现说明给执行者**：上面的 `_collect_extract`、`_review_one`、`_review_collect` 需要你补齐。要求：

- `_collect_extract(state, runtime) -> dict`：扇出的 `drafts` 已由 reducer 汇总，此处只负责落盘 `02-drafts.json`，返回 `{}`。**注意** `PipelineState.drafts` 目前是覆盖语义，扇出需要它变成累加——请把 `graph.py` 里 `PipelineState` 的 `drafts` 改为 `Annotated[list[TopicDraft], operator.add]`，并**同步检查 A 阶段是否受影响**（A 阶段只有一个 Node 写 drafts，改成累加后行为不变，但要跑测试确认）。
- `_review_one` / `_review_collect`：把 `node_review` 拆成「逐 draft 判 fidelity + name_desc」与「汇总 + 边过滤 + 边审核 + 孤儿检测」两段。边相关的逻辑**不扇出**（需要全局 kept_ids）。

- [ ] **Step 4: 跑测试确认通过**

```bash
uv run pytest tests/pipeline/test_graph_fanout.py -q
```
Expected: PASS

- [ ] **Step 5: 跑全量确认 A 阶段没被 `drafts` 改动弄坏**

```bash
uv run pytest -q
```
Expected: 全绿

- [ ] **Step 6: 提交**

```bash
git add src/cn_curriculum_graph/pipeline/graph_fanout.py src/cn_curriculum_graph/pipeline/graph.py tests/pipeline/test_graph_fanout.py
git commit -m "feat(pipeline): B 阶段 Send 扇出到条目级

extract 与 review 扇出，dedupe/edges 保持整批（需要全局视野）。
checkpoint 粒度从层变成条目：恢复时只重跑失败那一条。
这已不是与手写版对等的实现，笔记里须与第一章分开读。"
```

---

### Task 8: 第二章数据 + 对比笔记成稿

**Files:**
- Modify: `scripts/compare_orchestration.py`（加 `fanout` 引擎）
- Create: `docs/langgraph-vs-handwritten.md`
- Modify: `README.md`

- [ ] **Step 1: 把 fanout 加进实验脚本**

在 `_invoke` 里加第三个分支：

```python
    elif engine == "fanout":
        from cn_curriculum_graph.pipeline.graph_fanout import run_pipeline_fanout

        run_pipeline_fanout(
            source, out, deps, model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="exp",
        )
```

并在 `main()` 的引擎列表里加 `"fanout"`。

- [ ] **Step 2: 跑第二章实验**

```bash
uv run python scripts/compare_orchestration.py | tee /tmp/compare-b.txt
```
Expected: 九行数据（3 场景 × 3 引擎）。重点看 `rate_limit` 那行：`fanout` 的 `recovery_calls` 应显著低于 `langgraph`。

- [ ] **Step 3: 写笔记**

创建 `docs/langgraph-vs-handwritten.md`，三章结构（设计文档 §7）：

- **第一章 对等对比**：贴 Task 6 的数据表；框架省了什么（重试/重入/超时各自的代码行数，用 Task 6 Step 6 量的数字）；强加了什么（msgpack 类型注册警告、checkpoint 不可人眼读、state schema 的表达约束、出错时调用栈变深、多一层版本依赖）
- **第二章 额外解锁**：贴本任务的数据；条目级 checkpoint 省下的重复调用量；**明确声明架构已变，不能与第一章混读**
- **第三章 什么时候不该用它**：结论必须由前两章数据支撑，不写观点性断言

**必须写进笔记的三条限制**（设计文档 §5、§10）：
1. 实验用 fake 而非真模型，测不出真实限流行为（真实 429 常伴 `Retry-After`、并发退避）
2. 自定义类型进 checkpoint 需注册 `allowed_msgpack_modules`，未来版本强制
3. langgraph 1.2 发布于 2026-05，API 仍在快速变化，本文数据基于 1.2.9

- [ ] **Step 4: 更新 README**

在「怎么跑」加 `--engine langgraph` 用法；在「下一步」把阶段四标记完成并指向笔记。

- [ ] **Step 5: 跑全量并提交**

```bash
uv run pytest -q
git add scripts/compare_orchestration.py docs/langgraph-vs-handwritten.md README.md
git commit -m "docs: 手写 vs LangGraph 对比笔记（路线图阶段四验收物）

三章：对等对比（数据）、额外解锁（数据）、什么时候不该用它（由数据支撑）。
三条限制已声明：fake 非真模型、msgpack 类型注册、API 变化快。"
```

---

## 自检记录

- **spec 覆盖**：§2 API 核实 → Task 4/5 直接使用；§3 三个域缺陷 → Task 1（CYCLE）、Task 2（孤儿 + CONSISTENCY_SKIPPED）；§4 状态模型与节点映射 → Task 4；§5 故障注入与度量 → Task 3（装置）+ Task 6（实验）；§6 B 阶段 → Task 7；§7 笔记结构 → Task 8；§8 测试策略（参数化到两引擎）→ Task 4 Step 6-7。无遗漏。
- **占位符**：无 TBD/TODO。Task 7 Step 3 的三个 `_` 函数是**有意留给执行者补齐**并给了明确要求 —— 这是本计划唯一的非完整代码块，因为扇出后的 review 拆分依赖 A 阶段落地后的实际结构，硬写会与实际对不上。执行时若发现要求不清晰，应升级询问而不是猜。
- **类型一致性**：`PipelineState`、`PipelineDeps`、`run_pipeline_lg`、`FaultSpec`、`CallCounter`、`ScenarioResult` 在各 Task 中命名一致；`filter_edges_by_kept_drafts` / `detect_orphans` 在 Task 2 定义、Task 4 使用，签名一致。
- **已知风险**：Task 7 会把 `PipelineState.drafts` 从覆盖改成累加，影响 A 阶段——已在该 Task 明确要求跑全量确认。
