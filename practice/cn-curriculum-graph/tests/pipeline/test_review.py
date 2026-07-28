"""审核层：分歧即淘汰。

本轮定位是不承诺内容专业正确，那就宁可少产出也别放可疑的进去。
被淘汰的那批写进 dropped.json —— 它是最值得人工复核的清单，比随机抽检有价值。

⚠️ 当前双票是同族（deepseek-v4-flash + v4-pro），独立性打折。
理想是跨训练谱系互投，配上 ANTHROPIC_API_KEY 即可切换。
"""

from types import SimpleNamespace

import pytest

from cn_curriculum_graph.pipeline.models import DraftContent, ProposedEdge, TopicDraft, Vote
from typing import get_args

from cn_curriculum_graph.pipeline.review import (
    FidelityJudgment,
    FidelityVerdict,
    FIDELITY_TOOL_NAME,
    EDGE_REVIEW_TOOL_NAME,
    DeepSeekEdgeJudge,
    DeepSeekFidelityJudge,
    detect_orphans,
    filter_edges_by_kept_drafts,
    review_drafts,
    review_edges,
)
from cn_curriculum_graph.validators.consistency import Verdict


def _draft(draft_id: str = "a", name: str = "小数的意义") -> TopicDraft:
    return TopicDraft(
        draft_id=draft_id,
        chunk_id="c#001",
        standard_codes=["3.1.2"],
        content=DraftContent(
            name=name,
            description="理解小数表示十进制分数",
            type="conceptual",
            subject="数学",
            domain="数与代数",
            grade_start=4,
            grade_end=4,
            evidence=["证据"],
            assessment_prompt="问一句",
            source_span="能理解小数的意义",
        ),
    )


def _fidelity(approved: bool, reviewer: str = "fake"):
    """二值替身，映射到三档：True→faithful，False→fabricated。

    保留这个 helper 而不是全量改写既有测试：它们表达的是"通过/不通过"这层
    语义，与档位无关，改成三档反而模糊了各自的测试意图。"""

    def judge(draft: TopicDraft) -> FidelityVerdict:
        return FidelityVerdict(
            reason="测试", judgment="faithful" if approved else "fabricated", reviewer=reviewer
        )

    return judge


def _name_judge(judgment: str):
    def judge(name: str, description: str) -> Verdict:
        return Verdict(judgment=judgment, reason="测试")

    return judge


def test_a_draft_approved_by_everyone_is_kept():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲"), _fidelity(True, "乙")],
        name_judges=[_name_judge("consistent")],
    )

    assert [d.draft_id for d in result.kept] == ["a"]
    assert result.drops == []


def test_split_fidelity_vote_drops_the_draft():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲"), _fidelity(False, "乙")],
        name_judges=[_name_judge("consistent")],
    )

    assert result.kept == []
    assert result.drops[0].reason == "REVIEW_REJECTED"
    assert "fidelity" in result.drops[0].detail


def test_topic_mismatch_drops_the_draft():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True)],
        name_judges=[_name_judge("topic_mismatch")],
    )

    assert result.kept == []
    assert "name_desc" in result.drops[0].detail


def test_scope_mismatch_keeps_the_draft_but_records_the_outcome():
    """范围不符是 WARNING 级 —— 保留，但要留痕，跟校验层的两级严重性一致。"""
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True)],
        name_judges=[_name_judge("scope_mismatch")],
    )

    assert [d.draft_id for d in result.kept] == ["a"]
    scoped = [o for o in result.outcomes if o.aspect == "name_desc"]
    assert scoped[0].approved is True
    assert "scope_mismatch" in scoped[0].votes[0].reason


def test_every_decision_is_recorded_even_when_approved():
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity(True, "甲")],
        name_judges=[_name_judge("consistent")],
    )

    assert {o.aspect for o in result.outcomes} == {"fidelity", "name_desc"}


def test_edges_are_reviewed_and_rejected_ones_dropped():
    def approve(target, prerequisite, edge):
        return Vote(reviewer="甲", approved=edge.prerequisite_draft_id == "good", reason="测试")

    edges = {
        "a": [
            ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住"),
            ProposedEdge(prerequisite_draft_id="bad", strength="soft", reason="站不住"),
        ]
    }

    result = review_edges(
        {"a": _draft("a"), "good": _draft("good"), "bad": _draft("bad")},
        edges,
        edge_judges=[approve],
    )

    assert [e.prerequisite_draft_id for e in result.kept_edges["a"]] == ["good"]
    assert result.drops[0].reason == "REVIEW_REJECTED"
    assert "bad" in result.drops[0].detail


def test_fidelity_judge_failure_drops_the_draft_conservatively_without_crashing():
    """设计文档 §6：单条目失败不中断整批。review 是全流水线调用量最高的一层
    （约为 extract 的 4-6 倍），任意一次限流/超时都会撞上；一次抛异常绝不能
    让 run_pipeline 整体崩掉、丢光前几层的 LLM 花费。判定器没能表态时，
    按本层"分歧即淘汰"的精神保守处理：该草稿判为未通过，而不是放行。"""

    def flaky_fidelity(draft: TopicDraft) -> FidelityVerdict:
        if draft.draft_id == "a":
            raise RuntimeError("429")
        return FidelityVerdict(reason="ok", judgment="faithful", reviewer="fake")

    other_draft = _draft("b", "另一个知识点")
    result = review_drafts(
        [_draft("a"), other_draft],
        fidelity_judges=[flaky_fidelity],
        name_judges=[_name_judge("consistent")],
    )

    # 出问题的那条被保守淘汰，记账原因码专门区分于普通 REVIEW_REJECTED
    assert "a" not in {d.draft_id for d in result.kept}
    fail_drops = [d for d in result.drops if d.reason == "FIDELITY_JUDGE_FAILED"]
    assert len(fail_drops) == 1
    assert fail_drops[0].ref == "a"
    assert "RuntimeError" in fail_drops[0].detail

    # 其余草稿不受影响，照常评审通过
    assert "b" in {d.draft_id for d in result.kept}


def test_name_judge_failure_drops_the_draft_conservatively_without_crashing():
    def flaky_name_judge(name: str, description: str):
        raise RuntimeError("timeout")

    result = review_drafts(
        [_draft("a")],
        fidelity_judges=[_fidelity(True)],
        name_judges=[flaky_name_judge],
    )

    assert result.kept == []
    fail_drops = [d for d in result.drops if d.reason == "NAME_JUDGE_FAILED"]
    assert len(fail_drops) == 1
    assert fail_drops[0].ref == "a"
    assert "RuntimeError" in fail_drops[0].detail


def test_edge_judge_failure_drops_the_edge_conservatively_without_crashing():
    def flaky_edge_judge(target, prerequisite, edge):
        raise RuntimeError("429")

    edges = {
        "a": [ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住")],
    }

    result = review_edges(
        {"a": _draft("a"), "good": _draft("good"), "bad": _draft("bad")},
        edges,
        edge_judges=[flaky_edge_judge],
    )

    assert result.kept_edges["a"] == []
    fail_drops = [d for d in result.drops if d.reason == "EDGE_JUDGE_FAILED"]
    assert len(fail_drops) == 1
    assert "a<-good" in fail_drops[0].ref
    assert "RuntimeError" in fail_drops[0].detail


def test_review_reraises_programming_errors_instead_of_recording_a_drop():
    """AttributeError/TypeError/NameError/KeyError 是程序 bug，不该被
    FIDELITY_JUDGE_FAILED/NAME_JUDGE_FAILED/EDGE_JUDGE_FAILED 悄悄吞掉。"""
    import pytest

    def buggy_fidelity(draft: TopicDraft) -> Vote:
        raise AttributeError("拼错了属性名")

    with pytest.raises(AttributeError):
        review_drafts(
            [_draft()], fidelity_judges=[buggy_fidelity], name_judges=[_name_judge("consistent")]
        )

    def buggy_name_judge(name: str, description: str):
        raise TypeError("参数不对")

    with pytest.raises(TypeError):
        review_drafts(
            [_draft()], fidelity_judges=[_fidelity(True)], name_judges=[buggy_name_judge]
        )

    def buggy_edge_judge(target, prerequisite, edge):
        raise KeyError("some_key")

    edges = {"a": [ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住")]}
    with pytest.raises(KeyError):
        review_edges(
            {"a": _draft("a"), "good": _draft("good")}, edges, edge_judges=[buggy_edge_judge]
        )


def test_review_edges_skips_unknown_target_without_crashing():
    """预先算好的 drafts_by_id 与 edges 字典键不一定同步（例如上游只传审核后
    幸存的 draft，edges 里却还留着已被淘汰目标的边）。查不到目标不能让整层
    崩掉（KeyError），也不能静默丢 —— 必须留一条带原因码的 DropRecord。"""

    edges = {
        "ghost": [ProposedEdge(prerequisite_draft_id="a", strength="hard", reason="站得住")],
    }

    result = review_edges(
        {},
        edges,
        edge_judges=[lambda target, prerequisite, edge: Vote(reviewer="甲", approved=True, reason="测试")],
    )

    assert result.kept_edges == {}
    assert result.drops[0].reason == "UNKNOWN_REVIEW_TARGET"
    assert "ghost" in result.drops[0].detail


def test_review_drafts_rejects_empty_fidelity_judges():
    """空 judges 列表不是合法输入而是配置错误：all([]) == True 会把零信息
    读成满票通过，与"分歧即淘汰"的设计哲学正好相反，必须当场炸而不是静默放行。"""
    with pytest.raises(ValueError, match="fidelity_judges"):
        review_drafts(
            [_draft()],
            fidelity_judges=[],
            name_judges=[_name_judge("consistent")],
        )


def test_review_drafts_rejects_empty_name_judges():
    with pytest.raises(ValueError, match="name_judges"):
        review_drafts(
            [_draft()],
            fidelity_judges=[_fidelity(True)],
            name_judges=[],
        )


def test_review_edges_rejects_empty_edge_judges():
    edges = {
        "a": [ProposedEdge(prerequisite_draft_id="good", strength="hard", reason="站得住")],
    }

    with pytest.raises(ValueError, match="edge_judges"):
        review_edges({"a": _draft("a")}, edges, edge_judges=[])


def _fake_client(recorder: dict, tool_input: dict):
    def create(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(
            content=[
                SimpleNamespace(type="tool_use", name=FIDELITY_TOOL_NAME, input=tool_input)
            ]
        )

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_deepseek_fidelity_judge_shows_both_description_and_source_span():
    recorder: dict = {}
    client = _fake_client(recorder, {"reason": "描述超出原文", "judgment": "fabricated"})

    verdict = DeepSeekFidelityJudge(client=client, model="deepseek-v4-pro")(_draft())

    assert verdict.is_faithful is False
    assert verdict.judgment == "fabricated"
    # reviewer 由代码填、不问模型 —— 但必须保留下来，否则跨模型分歧分析会失明
    assert verdict.reviewer == "deepseek-v4-pro"
    payload = str(recorder["messages"])
    assert "理解小数表示十进制分数" in payload
    assert "能理解小数的意义" in payload
    assert recorder["tool_choice"] == {"type": "tool", "name": FIDELITY_TOOL_NAME}


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


# ─────────────────────────────────────────────────────────────────────────────
# fidelity 三档化（2026-07-27）
#
# 动机来自 28 条真实课标语料的一次全量运行：72 个 draft 里 45 个被 fidelity
# 淘汰（62%），而这 45 条里 **21 条（47%）是分歧淘汰** —— 两个模型看法相反。
# 分歧的样子说明了一切：
#
#   原文「理解数位的含义」→ 描述「理解个位、十位…的含义」
#       flash ✅「合理展开」   pro ❌「凭空增加」
#   原文「会比较万以内数的大小」→ 描述「…掌握从高位到低位逐位比较」
#       flash ❌「额外增加」   pro ✅「合理具体化」
#
# **注意后一条的方向是反的。** 不是两个模型各有稳定立场，是"合理展开算不算
# 忠实"这个判断在二值框架下没有稳定答案 —— 模型每次随机塞进一个格子。
#
# 这是 memory 里那条教训的第二次实证（第一次是 name/desc judge 从二值扩到
# 三档）：**判定档位不够时，模型不会告诉你"没有合适的选项"，它会硬塞进现有
# 某一档。** 缺的第三档是「原文的合理具体化」。
#
# 更深一层的矛盾（三档要解决的根本问题）：课标条目是纲领性文字（"理解数位的
# 含义"），而知识依赖图的节点必须是可教、可测的具体知识点。要求 description
# 严格忠于 source_span，等于要求节点停留在纲领的抽象层级 —— 那样的节点没法用。
# **忠实性和有用性在课标这种文本上直接冲突**，二值判定无法表达这个冲突。
# ─────────────────────────────────────────────────────────────────────────────


def _fidelity_v(judgment: str, reviewer: str = "fake"):
    """三档 fidelity 判定器替身。"""

    def judge(draft: TopicDraft) -> FidelityVerdict:
        return FidelityVerdict(reason=f"{reviewer}说{judgment}", judgment=judgment)

    return judge


def test_fidelity_verdict_has_three_tiers_not_two():
    """档位数是产品决策，不是实现细节 —— 钉死它。"""
    assert set(get_args(FidelityJudgment)) == {
        "faithful",
        "reasonable_elaboration",
        "fabricated",
    }


def test_only_fabricated_drops_the_draft():
    """三档 → 两级后果：faithful / reasonable_elaboration 都保留，只有
    fabricated 淘汰。这正是把 45 条淘汰里那 21 条灰区救回来的那一步。"""
    kept = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity_v("reasonable_elaboration")],
        name_judges=[_name_judge("consistent")],
    )
    assert len(kept.kept) == 1, "合理展开不该被淘汰"

    dropped = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity_v("fabricated")],
        name_judges=[_name_judge("consistent")],
    )
    assert dropped.kept == []
    assert dropped.drops[0].reason == "REVIEW_REJECTED"


def test_reasonable_elaboration_is_kept_but_leaves_a_trace():
    """保留 ≠ 无声通过。合理展开必须在 review-log 里留痕，否则"这个节点的描述
    比原文具体"这件事就永远没人知道 —— 与 scope_mismatch 的处理一致
    （见 test_scope_mismatch_keeps_the_draft_but_records_the_outcome）。"""
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity_v("reasonable_elaboration", "甲")],
        name_judges=[_name_judge("consistent")],
    )

    fid = [o for o in result.outcomes if o.aspect == "fidelity"]
    assert len(fid) == 1
    assert fid[0].approved is True
    assert fid[0].votes[0].judgment == "reasonable_elaboration", (
        "档位要能被程序读出来，不能只埋在 reason 字符串里"
    )


def test_disagreement_between_tiers_still_drops_conservatively():
    """一个判 faithful、一个判 fabricated —— 分歧即淘汰的原则不变。
    三档化解决的是"灰区被随机塞格子"，不是放松分歧处理。"""
    result = review_drafts(
        [_draft()],
        fidelity_judges=[_fidelity_v("faithful", "甲"), _fidelity_v("fabricated", "乙")],
        name_judges=[_name_judge("consistent")],
    )

    assert result.kept == []


def test_fidelity_tool_schema_puts_reason_before_judgment():
    """字段顺序不是排版偏好 —— 工具调用是顺序生成的，`judgment` 放在 `reason`
    前面等于让模型**先投票再找理由**。旧的 `_VotePayload` 正是 approved 在前。

    把 reason 放前面，是结构化输出里 CoT 的等价物：先写依据，再落判定。
    （说明：这一条是设计改进，不是上面那次分歧统计支持的结论 —— 那组数据
    证明的是档位不够，不是字段顺序有害。这里单独修，效果另测。）
    """
    props = list(FidelityVerdict.model_json_schema()["properties"].keys())

    assert props.index("reason") < props.index("judgment")


# ─────────────────────────────────────────────────────────────────────────────
# 边审核并发化（2026-07-27）
#
# 起因是一次真实失败：三档 fidelity 把 draft 存活率从 27/72 提到 64/72 之后，
# 边审核的工作量从"几乎没有"（上一轮 292 条边在审核前就随端点连坐死了）
# 变成 345 条边 × 2 判定器 = **690 次串行调用**，直接撞上 `NODE_TIMEOUT`：
#
#   NodeTimeoutError: Node 'review' exceeded its run timeout of 600.000s
#   总耗时 49:32（超时被 RetryPolicy 重跑了 3 次）
#
# 这一次失败同时引爆了三条此前只活在文档与合成测试里的结论：
#   ① NODE_TIMEOUT 第一次在真实运行里打响；
#   ② RetryPolicy 重试了 NodeTimeoutError —— 整层重跑 3 次，白烧三倍调用；
#   ③ 总耗时 49:32 ≫ 10 分钟超时 —— 印证"timeout 不提供墙钟上界"。
#
# **根因是同一个**：并发校准只覆盖了 graph_fanout 的两个扇出点
# （extract_one / review_one），`review_edges` 从来没被并发化过 ——
# 只是以前它没活干，所以没人发现。
#
# 关键认知：`review_edges` 需要全局 `kept_ids` 才能**开始**，但开始之后
# **每条边的判定是彼此独立的**。"需要全局视野"阻止的是**扇出**（Send 到
# 条目级），不是层内并发。
# ─────────────────────────────────────────────────────────────────────────────


def test_review_edges_runs_judges_concurrently():
    """690 次调用必须并发，否则串行下真实语料必然超时。"""
    import threading
    import time

    lock = threading.Lock()
    state = {"now": 0, "peak": 0}

    def slow_judge(target, prerequisite, edge):
        with lock:
            state["now"] += 1
            state["peak"] = max(state["peak"], state["now"])
        time.sleep(0.05)
        with lock:
            state["now"] -= 1
        return Vote(reviewer="fake", approved=True, reason="ok")

    edges = {
        "a": [
            ProposedEdge(prerequisite_draft_id=f"p{i}", strength="hard", reason="理由")
            for i in range(8)
        ]
    }

    drafts = {"a": _draft("a")} | {f"p{i}": _draft(f"p{i}") for i in range(8)}
    review_edges(drafts, edges, edge_judges=[slow_judge], max_concurrency=4)

    assert state["peak"] > 1, "边审核仍是串行的"
    assert state["peak"] <= 4, f"并发上限没生效，峰值 {state['peak']}"


def test_review_edges_output_is_identical_to_serial_order():
    """并发不能改变产物顺序。

    两个编排引擎的对等性测试是**逐字节比对落盘产物**的
    （test_two_engines_produce_identical_artifacts_on_normal_run）；
    判定完成的先后顺序一旦泄漏进 kept_edges / outcomes / drops 的排列，
    那组测试就会变成随机红绿 —— 比"慢"糟糕得多。
    """
    import random
    import time

    def jittery_judge(target, prerequisite, edge):
        # 故意让完成顺序与输入顺序无关
        time.sleep(random.uniform(0, 0.02))
        ok = edge.prerequisite_draft_id != "p3"
        return Vote(reviewer="fake", approved=ok, reason="ok")

    edges = {
        "a": [
            ProposedEdge(prerequisite_draft_id=f"p{i}", strength="hard", reason="理由")
            for i in range(6)
        ]
    }

    drafts = {"a": _draft("a")} | {f"p{i}": _draft(f"p{i}") for i in range(6)}
    serial = review_edges(drafts, edges, edge_judges=[jittery_judge], max_concurrency=1)
    parallel = review_edges(drafts, edges, edge_judges=[jittery_judge], max_concurrency=6)

    assert [e.prerequisite_draft_id for e in parallel.kept_edges["a"]] == [
        e.prerequisite_draft_id for e in serial.kept_edges["a"]
    ]
    assert [o.target for o in parallel.outcomes] == [o.target for o in serial.outcomes]
    assert [d.ref for d in parallel.drops] == [d.ref for d in serial.drops]


def test_review_edges_still_reraises_programming_errors_when_concurrent():
    """并发不能把程序 bug 吞成 EDGE_JUDGE_FAILED —— 线程池会把异常包起来，
    必须显式还原成原类型冒泡，否则 PROGRAMMING_ERRORS 那道防线在并发路径上失效。"""

    def buggy(target, prerequisite, edge):
        raise KeyError("some_key")

    edges = {"a": [ProposedEdge(prerequisite_draft_id="p", strength="hard", reason="理由")]}

    with pytest.raises(KeyError):
        review_edges(
            {"a": _draft("a"), "p": _draft("p")}, edges,
            edge_judges=[buggy], max_concurrency=4,
        )


# --- 边审核必须看得见前置知识点本身，而不只是一个 id -------------------
#
# 2026-07-28 诊断出来的 bug：边审核的 prompt 只传了目标的 name+description，
# 前置只传了 draft_id 这个不透明字符串。也就是让模型判断"A 是不是 B 的前置"，
# 却只告诉它 B 是什么。实测后果：deepseek-v4-flash 在这个任务上通过率塌到
# 12%（它在 topic 的 fidelity 上是 99%），65% 的否决理由是"未提供该知识点的
# 具体名称和内容，无法判断"；而 deepseek-v4-pro 之所以还能表态，是从边自带
# 的 reason 里反推出前置是谁 —— 用待验证的说法去认定待验证的对象。
# 规则是"分歧即淘汰"，于是 flash 一个人把边存活率钉死在 12%（31/269）。


def test_edge_judge_receives_the_prerequisite_draft_not_just_its_id():
    """判定器必须拿到前置的 name 与 description。

    只给 id 时模型没法判断先修关系是否成立，只能要么拒绝表态、要么从
    `edge.reason` 里反推 —— 后者是拿待验证的说法去认定待验证的对象。
    """
    seen: list[tuple[str, str]] = []

    def spy(target, prerequisite, edge):
        seen.append((prerequisite.draft_id, prerequisite.content.name))
        return Vote(reviewer="甲", approved=True, reason="测试")

    edges = {"a": [ProposedEdge(prerequisite_draft_id="p", strength="hard", reason="理由")]}

    review_edges(
        {"a": _draft("a"), "p": _draft("p", name="计数单位的感悟")},
        edges,
        edge_judges=[spy],
    )

    assert seen == [("p", "计数单位的感悟")]


def test_edge_with_unknown_prerequisite_is_dropped_with_a_record_not_a_crash():
    """前置查不到时按 target 缺失的同一套路处理：留痕跳过，不 KeyError。

    正常流程里 `filter_edges_by_kept_drafts` 已经剔掉了前置被淘汰的边，
    所以这条路径不该被走到 —— 但 `drafts_by_id` 与 `edges` 不保证同步是这个
    函数 docstring 里写明的前提，target 那侧已有 UNKNOWN_REVIEW_TARGET 兜着，
    前置这侧不能反而崩掉。
    """
    edges = {"a": [ProposedEdge(prerequisite_draft_id="missing", strength="hard", reason="理由")]}

    result = review_edges({"a": _draft("a")}, edges, edge_judges=[_never_called])

    assert result.kept_edges["a"] == []
    assert [d.reason for d in result.drops] == ["UNKNOWN_EDGE_PREREQUISITE"]
    assert "missing" in result.drops[0].detail


def _never_called(target, prerequisite, edge):
    raise AssertionError("前置查不到时不该调用判定器")


def test_deepseek_edge_prompt_carries_the_prerequisite_name_and_description():
    """钉住真实 prompt 的内容 —— 这个 bug 的现场就在这段字符串里。"""
    captured: dict[str, str] = {}

    class _FakeClient:
        class messages:  # noqa: N801
            @staticmethod
            def create(**kwargs):
                captured["prompt"] = kwargs["messages"][0]["content"]
                return SimpleNamespace(
                    content=[
                        SimpleNamespace(
                            type="tool_use",
                            name=EDGE_REVIEW_TOOL_NAME,
                            input={"approved": True, "reason": "ok"},
                        )
                    ]
                )

    DeepSeekEdgeJudge(client=_FakeClient())(
        _draft("a", name="分数的意义"),
        _draft("p", name="计数单位的感悟"),
        ProposedEdge(prerequisite_draft_id="p", strength="hard", reason="理由"),
    )

    assert "计数单位的感悟" in captured["prompt"]
    assert "理解小数表示十进制分数" in captured["prompt"]  # 前置的 description
