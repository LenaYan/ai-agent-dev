"""checkpoint 序列化的 msgpack 类型 allowlist。

**这一组测试推翻了本项目此前记在 `docs/langgraph-vs-handwritten.md` 与
README 里的一条判断**——"未注册 `allowed_msgpack_modules`，升级 langgraph
会让 checkpoint 整个失效"。实测（langgraph 1.2.9 / langgraph-checkpoint
4.1.1，2026-07-27）两点都不成立：

1. **框架已经替你注册了**：`StateGraph.compile(checkpointer=...)` 会调用
   `langgraph._internal._serde.build_serde_allowlist(schemas=[state_schema])`
   递归遍历 state schema 的类型标注，把其中的 pydantic 模型/dataclass/Enum
   收进 allowlist，再用 `checkpointer.with_allowlist(...)` 返回一个**带
   allowlist 的克隆**给 Pregel 用。本项目 11 个自定义类型全部被它收全。
   注意"克隆"这个细节：`AsyncSqliteSaver.from_conn_string()` 拿到的那个
   裸 saver 自己**没有** allowlist，直接拿它 `aget_tuple()` 会看到降级后的
   dict——这曾经让我误判成"续跑会拿到 dict"，实际执行路径用的是 compile
   返回的克隆，拿到的是真模型。
2. **未注册的失败模式不是"失效"，是静默降级**：`jsonplus.py` 的 ext hook
   在 `_check_allowed` 返回 False 时走的是 `return tup[2]`——把 pydantic
   模型还原成它的 kwargs **dict**，不抛异常、不返回 None。也就是说漏注册
   一个类型不会得到一句"checkpoint 坏了"，而是下游某个 Node 拿着 dict 去
   访问 `.draft_id`，炸出一个看起来像代码 bug 的 `AttributeError`（而
   `AttributeError` 正好在 `PROGRAMMING_ERRORS` 里，会被 `retry_on` 排除、
   直接冒泡）——根因在序列化配置，症状在业务代码，排查方向完全是错的。

因此本项目的实际动作不是"手写一份 allowlist"（那反而更差：手写的列表会
和 state schema 漂移，而框架的推导不会），而是：
- **主动开启严格模式**（`build_checkpoint_serde()`，见 graph.py），让未来
  版本的默认行为今天就生效，漏注册当场暴露而不是留到升级那天；
- 用下面的行为测试把"严格模式下崩溃续跑产物仍然逐字节一致"锁死；
- 用 `test_a_loosely_typed_channel_is_not_covered_by_auto_derivation` 证明
  这层保护**有前提**（每个进 checkpoint 的值，其类型必须在 state schema 上
  精确声明），不是无条件成立的空话。
"""

from __future__ import annotations

import hashlib
from typing import Any, TypedDict

import pytest
from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer
from langgraph.graph import END, START, StateGraph

from langgraph.checkpoint.memory import InMemorySaver

from cn_curriculum_graph.pipeline import graph as graph_mod
from cn_curriculum_graph.pipeline.graph import (
    apply_state_allowlist,
    build_checkpoint_serde,
    build_graph,
    run_pipeline_lg,
)
from cn_curriculum_graph.pipeline.graph_fanout import build_fanout_graph, run_pipeline_fanout
from cn_curriculum_graph.pipeline.models import DraftContent, TopicDraft

from .test_run import _fake_deps

ARTIFACTS = ("graph.json", "05-reviewed.json", "03-deduped.json", "04-edges.json")

# monkeypatch.undo() 会把本次测试里**全部** setattr 一起撤销，包括
# apply_state_allowlist 的顶替——那正是被测对象，撤销了这条测试就作废了。
# 所以留一份原函数引用，单独还原 propose_all。
_real_propose_all = graph_mod.edges_mod.propose_all


class _Boom(Exception):
    """制造一次"整层不可恢复失败"，逼出 checkpoint 续跑路径。"""


def _write_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "m.md").write_text("3.1.1 甲。\n\n3.1.2 乙。\n", encoding="utf-8")
    return source


def _digest(out_dir) -> dict[str, str]:
    return {
        name: hashlib.sha256((out_dir / name).read_bytes()).hexdigest()
        for name in ARTIFACTS
        if (out_dir / name).exists()
    }


def test_project_serde_is_strict_not_permissive():
    """本项目显式选择严格模式，而不是沿用 langgraph 当前的宽松默认。

    宽松默认（`allowed_msgpack_modules=True`）下，任何未注册类型都会被
    "警告一句然后照常还原"，`with_allowlist()` 对它是彻底的 no-op（见
    `JsonPlusSerializer.with_msgpack_allowlist`：`base_allowlist is True`
    时直接 `return self`）。也就是说宽松模式下 allowlist 根本没被记录，
    等 langgraph 把默认翻成严格的那天，漏掉的类型才第一次现形——那时候
    出问题的是生产环境，不是测试。

    传一个空 allowlist 让基线变成"只允许内置安全类型"，compile 时框架再把
    state schema 推导出的类型合并进来（见下一条测试）。
    """
    serde = build_checkpoint_serde()

    assert serde._allowed_msgpack_modules is not True, (
        "宽松模式下 with_allowlist() 是 no-op，等于没有任何注册"
    )


def test_langgraph_still_exposes_the_allowlist_derivation_helper():
    """本项目唯一一处 import langgraph 私有 API 的绊线。

    `apply_state_allowlist` 依赖 `langgraph._internal._serde.
    build_serde_allowlist`。选它而不是自己手写一份类型遍历，是为了跟框架
    的推导规则保持一致（它处理 Annotated/Union/NewType/dataclass/pydantic
    v1&v2/Enum 等一堆边角）。代价是私有 API 随时可能挪窝——这条测试就是
    那根绊线：升级 langgraph 后它先红，而不是让 allowlist 静默变空、退化
    成"严格拦截 + 零注册"那个最坏组合。
    """
    from langgraph._internal._serde import build_serde_allowlist

    derived = build_serde_allowlist(schemas=[graph_mod.PipelineState])

    assert ("cn_curriculum_graph.pipeline.models", "TopicDraft") in derived


@pytest.mark.parametrize("builder", [build_graph, build_fanout_graph], ids=["A", "B-fanout"])
def test_apply_state_allowlist_registers_every_checkpointed_type(builder):
    """严格基线 + 显式推导 = 11 个自定义类型全部在册。

    这里刻意**不**拿框架自己的 `build_serde_allowlist` 当期望值（那是拿被
    测对象证明被测对象）。期望值是一份独立的字面量清单，来源是"哪些类型
    会真的进 checkpoint"这个人工判断；框架推导规则若发生变化、或有人往
    state 里加了新类型忘了它是 pydantic 模型，这条会失败。

    `PipelineDeps` 也会被收进去（`context_schema` 是 dataclass），但它从不
    进 checkpoint，多注册一个类型无害，故不在断言里挑出来。
    """
    b = builder()
    saver = apply_state_allowlist(b, InMemorySaver(serde=build_checkpoint_serde()))
    allowed = saver.serde._allowed_msgpack_modules
    ours = {
        k
        for k in allowed
        if k[0].startswith("cn_curriculum_graph") and k[1] != "PipelineDeps"
    }

    assert ours == {
        ("cn_curriculum_graph.models", "Misconception"),
        ("cn_curriculum_graph.pipeline.models", "Chunk"),
        ("cn_curriculum_graph.pipeline.models", "DraftContent"),
        ("cn_curriculum_graph.pipeline.models", "DropRecord"),
        ("cn_curriculum_graph.pipeline.models", "Merge"),
        ("cn_curriculum_graph.pipeline.models", "ProposedEdge"),
        ("cn_curriculum_graph.pipeline.models", "ReviewOutcome"),
        ("cn_curriculum_graph.pipeline.models", "TopicDraft"),
        ("cn_curriculum_graph.pipeline.models", "Vote"),
        ("cn_curriculum_graph.validators.base", "Finding"),
        ("cn_curriculum_graph.validators.base", "Severity"),
    }


@pytest.mark.parametrize(
    "runner", [run_pipeline_lg, run_pipeline_fanout], ids=["A", "B-fanout"]
)
def test_crash_and_resume_under_strict_serde_produces_identical_artifacts(
    runner, tmp_path, monkeypatch
):
    """核心行为测试：严格模式下"崩一次再续跑"的产物必须和一口气跑完逐字节一致。

    这条比任何"allowlist 里有没有某个名字"的断言都强——漏注册任何一个类型，
    续跑时该字段会退化成 dict，要么下游 `AttributeError`、要么装配出内容
    不同的 graph.json，两种情况这里都会红。它也是唯一能覆盖到"没写进 state
    schema 但确实进了 checkpoint"（例如 `Send` 的 payload）那类值的测试。
    """
    source = _write_source(tmp_path)

    # 参照组：不中断，一口气跑完
    baseline_out = tmp_path / "baseline"
    runner(source, baseline_out, _fake_deps(), model_id="fake", curriculum="c")

    # 实验组：edges 层整层崩溃 -> 续跑
    resumed_out = tmp_path / "resumed"
    db = tmp_path / "cp.sqlite"

    def boom(drafts, proposer):
        raise _Boom("模拟 edges 层不可恢复失败，逼出 checkpoint 续跑")

    monkeypatch.setattr(graph_mod.edges_mod, "propose_all", boom)
    with pytest.raises(Exception):
        runner(
            source, resumed_out, _fake_deps(), model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="t-serde",
        )
    monkeypatch.undo()

    runner(
        source, resumed_out, _fake_deps(), model_id="fake", curriculum="c",
        checkpoint_db=db, thread_id="t-serde",
    )

    assert _digest(resumed_out) == _digest(baseline_out)


@pytest.mark.parametrize(
    "runner", [run_pipeline_lg, run_pipeline_fanout], ids=["A", "B-fanout"]
)
def test_resumed_state_holds_real_models_not_degraded_dicts(runner, tmp_path, monkeypatch):
    """产物一致还不够——要直接盯住"跨越 checkpoint 之后，Node 拿到的到底是
    模型还是 dict"。

    产物一致有可能是巧合（比如降级发生在一个恰好不影响最终装配的字段上）；
    这里在 edges 层入口探针化，断言它从 state 里读到的 `deduped` 是真的
    `TopicDraft`。这正是漏注册时第一个出事的地方：`propose_all` 会去访问
    `d.content.name`，dict 上没有这个属性。
    """
    source = _write_source(tmp_path)
    db = tmp_path / "cp.sqlite"
    original = graph_mod.edges_mod.propose_all

    def boom(drafts, proposer):
        raise _Boom("先崩一次，把 deduped 留在 checkpoint 里")

    monkeypatch.setattr(graph_mod.edges_mod, "propose_all", boom)
    with pytest.raises(Exception):
        runner(
            source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="t-types",
        )
    monkeypatch.undo()

    seen: list[type] = []

    def spy(drafts, proposer):
        seen.extend(type(d) for d in drafts)
        return original(drafts, proposer)

    monkeypatch.setattr(graph_mod.edges_mod, "propose_all", spy)
    runner(
        source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c",
        checkpoint_db=db, thread_id="t-types",
    )

    assert seen, "edges 层没被调用，这条测试什么都没验证到"
    assert set(seen) == {TopicDraft}, f"跨 checkpoint 后拿到的是 {set(seen)}，不是 TopicDraft"


def test_forgetting_apply_state_allowlist_breaks_resume(tmp_path, monkeypatch):
    """`apply_state_allowlist` 是承重的，不是装饰——把它拿掉，续跑当场炸。

    这条锁的是本次实现踩过的那个坑（详见 `apply_state_allowlist` 文档里的
    三行表格）：**只把 serde 换成严格、指望 `compile()` 自动补 allowlist，
    是三种配置里最糟的一种**——框架那段自动推导整个被 `if
    STRICT_MSGPACK_ENABLED:` 门控，环境变量没开时它压根不执行，于是严格
    拦截照常生效、注册表却是空的。

    这里通过把 `apply_state_allowlist` 顶替成恒等函数来模拟"有人图省事
    删了这一行"，断言续跑会以 `AttributeError` 失败。没有这条测试，未来
    任何一次"这行看着没用，删了吧"的重构都会静默地把 checkpoint 续跑变成
    一颗定时炸弹（happy path 全绿，只有真正崩溃过一次的那条路径会炸）。
    """
    source = _write_source(tmp_path)
    db = tmp_path / "cp.sqlite"

    def boom(drafts, proposer):
        raise _Boom("先崩一次，把 deduped 留在 checkpoint 里")

    monkeypatch.setattr(graph_mod.edges_mod, "propose_all", boom)
    monkeypatch.setattr(graph_mod, "apply_state_allowlist", lambda builder, saver: saver)
    with pytest.raises(Exception):
        run_pipeline_lg(
            source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="t-noallow",
        )
    monkeypatch.setattr(graph_mod.edges_mod, "propose_all", _real_propose_all)

    with pytest.raises(AttributeError, match="'dict' object has no attribute"):
        run_pipeline_lg(
            source, tmp_path / "out", _fake_deps(), model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="t-noallow",
        )


def test_unregistered_type_degrades_to_dict_instead_of_failing_loudly():
    """把"漏注册的真实后果"钉死成一条会失败的断言，而不是留在注释里。

    这条测试的价值在于它记录的是**反直觉的那一半**：安全加固的失败模式
    通常是"拒绝服务"，而这里是"静默换类型"。知道这一点，将来在续跑路径上
    看到莫名其妙的 `AttributeError: 'dict' object has no attribute 'xxx'`
    才会想到往序列化配置上查，而不是去 debug 业务代码。
    """
    draft = TopicDraft(
        draft_id="d1", chunk_id="c1", standard_codes=["3.1.1"],
        content=DraftContent(
            name="分数的初步认识", description="描述", type="conceptual",
            subject="数学", domain="数与代数", grade_start=3, grade_end=3,
            evidence=["能举例"], assessment_prompt="说说？", source_span="原文",
        ),
    )
    strict_without_our_types = JsonPlusSerializer(allowed_msgpack_modules=())

    restored = strict_without_our_types.loads_typed(
        strict_without_our_types.dumps_typed([draft])
    )

    assert isinstance(restored[0], dict), "漏注册时应当降级成 dict"
    assert restored[0]["draft_id"] == "d1", "降级后数据仍在，只是类型没了 —— 所以才难发现"


def test_a_loosely_typed_channel_is_not_covered_by_auto_derivation():
    """框架的自动推导有前提：类型必须在 state schema 上**精确声明**。

    这条测试用一个 `Any` 标注的 channel 证明前提是真前提——同一个
    `TopicDraft`，声明成 `list[TopicDraft]` 就被收进 allowlist，声明成
    `Any` 就收不到、严格模式下会降级成 dict。所以上面几条测试不是"框架
    永远兜底"的免死金牌，而是"只要我们坚持把 state 的每个字段标注到位，
    框架就会兜底"。往 `PipelineState` 里加一个 `Any`/裸 `dict` 字段，
    这层保护就在那个字段上失效了。
    """

    class _LooseState(TypedDict, total=False):
        payload: Any

    g = StateGraph(_LooseState)
    g.add_node("noop", lambda s: {})
    g.add_edge(START, "noop")
    g.add_edge("noop", END)

    saver = apply_state_allowlist(g, InMemorySaver(serde=build_checkpoint_serde()))
    allowed = saver.serde._allowed_msgpack_modules

    assert ("cn_curriculum_graph.pipeline.models", "TopicDraft") not in allowed
