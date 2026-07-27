"""LangGraph 版编排（A 阶段：一层一个 Node，与手写版对等）。

与 run.py 的关系：**共用同一套六层纯函数**，只有编排方式不同。
这是路线图阶段四「同一需求分别用手写和框架实现」的框架半边。

Node 里只做「取 state → 调那层已有函数 → 返回 delta」，
不写任何业务逻辑 —— 否则对比就变成"手写版 vs 框架版+重构"，不公平。

已核实 API（langgraph 1.2.9，2026-07-26 实测）：节点签名 (state, runtime)，
运行时依赖走 runtime.context，不进 checkpoint。

Task 5 追加实测（同一 langgraph 1.2.9）：
- 每 Node `timeout` 只在**异步执行路径**上受支持——同步 `.invoke()` 落到的
  `pregel/_retry.py::run_with_retry` 只要 `task.timeout is not None` 就
  无条件 `raise sync_timeout_unsupported`，哪怕 Node 函数本身已经是
  `async def` 也一样；只有 `.ainvoke()` 落到的 `arun_with_retry` 才真正
  遵守 timeout。因此 extract/dedupe/edges/review 四个挂了
  `retry_policy`/`timeout` 的 Node 都改成了 `async def`，且 `run_pipeline_lg`
  内部改用 `asyncio.run(app.ainvoke(...))`，对外仍保持同步签名。
- `SqliteSaver`（`langgraph.checkpoint.sqlite`）显式不支持 async 方法
  （`aget_tuple` 等直接 `raise NotImplementedError`，实测报错原文见
  `.superpowers/sdd/task-5-report.md`），既然编排走的是 ainvoke，
  checkpointer 必须用 `AsyncSqliteSaver`（`langgraph.checkpoint.sqlite.aio`，
  依赖 `aiosqlite`，已随 `langgraph-checkpoint-sqlite` 一并装好，
  pyproject 不需要再加一行）。这两点都与 Task 5 brief 原稿的代码不同，
  是被编译期/运行期报错逼出来的调整，不是我自己的偏好。
- **C1 修正（同一 Task 5，被 code review 打回后补）**：上面两点最初实现时，
  这四个 Node 的函数体虽是 `async def` 却完全没有 `await`——直接同步调用
  六层函数，这会让 `timeout` 参数编译期能过、运行期却永远不生效（事件
  循环被阻塞调用整个占住，看门狗 task 拿不到调度机会）。已实测复现并修复：
  四层内部对六层函数的调用统一改成 `await asyncio.to_thread(...)`，细节和
  代价见 `NODE_TIMEOUT` 定义处的注释。这里更正一句，避免误导：`async def`
  是编译期硬要求没错，但"函数体不需要 await"是不成立的——那正是本次 C1
  要修的地方。
"""

from __future__ import annotations

import asyncio
import operator
from datetime import timedelta
from pathlib import Path
from typing import Annotated, TypedDict

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from cn_curriculum_graph.pipeline import assemble as assemble_mod
from cn_curriculum_graph.pipeline import chunk as chunk_mod
from cn_curriculum_graph.pipeline import dedupe as dedupe_mod
from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline import extract as extract_mod
from cn_curriculum_graph.pipeline import io
from cn_curriculum_graph.pipeline import review as review_mod
from cn_curriculum_graph.pipeline.models import (
    PROGRAMMING_ERRORS,
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

# **S4 修复**：PROGRAMMING_ERRORS 的权威定义在 pipeline/models.py（extract.py /
# dedupe.py / edges.py / review.py 四个模块都从那里 import）。这里改成同样
# import 而不是自己再字面重复一份 —— 之前这里维护了一份内容相同的独立拷贝，
# 今天两份一样不代表以后也同步：往权威定义里加一个类型，不会魔法般同步到
# 一份独立拷贝上，retry_on 会在不知不觉中用着一份过期的排除集合。

# **S3 修复**：Node 级重试要排除的异常集合，在六层函数共用的 PROGRAMMING_ERRORS
# 之外，再加一个 ValueError。动机：review.py 里三处"空 judges 列表"哨兵
# （review_drafts ×2、review_edges ×1）抛的正是 ValueError，且是在批处理
# 循环开始之前就抛出的确定性配置错误（忘了传 judges），不是"这一条数据/
# 这次调用恰好失败了"。RETRY_POLICY 的注释自己写着"程序 bug 不该被重试——
# 重试三次只会把同一个 bug 犯三遍"，但收窄的 PROGRAMMING_ERRORS 里没有
# ValueError，导致这类配置错误被 Node 级 RetryPolicy 原样重犯了三遍——
# 已实测：见 test_retry_on_excludes_value_error_to_avoid_repeating_config_
# mistakes，RED 时 review_drafts 被调用 3 次。
# **订正（此前这里写过"白烧约 180 次真实 LLM 调用"，不实——已删除）**：
# review.py 里这三处空 judges 哨兵都在 for 循环之前就 raise（见
# review_drafts/review_edges 顶部），不会执行任何一次真实 judge/LLM 调用，
# 不管生产规模多大。重跑三遍的真实代价是"3 次哨兵 raise + RetryPolicy 的
# 退避等待"——不是重复的 LLM 调用。真正会被重复浪费的场景是六层函数整体
# 抛出的非哨兵错误（见下面 I1 一节：整层重跑会真的把已成功的条目也重新
# 处理一遍，那才是有真实 LLM 调用代价的地方）。
#
# **我的取舍（ValueError 的边界，S3 明确要求想清楚）**：这里选择的是"全部
# ValueError 都不重试"，而不是只精确排除"空 judges"这一种情形（那样做需要
# 一个自定义异常类型或错误码来区分，改动更大，且当前六层函数确实统一用
# ValueError 表达"确定性配置/契约错误"这一类语义，见 assemble.py 的 id
# 碰撞检查、review.py 的空 judges 检查——它们都不是"重试一次也许会好"的
# 瞬时故障）。这个取舍的已知代价：
# 1. pydantic 的 ValidationError 是 ValueError 的子类。若它意外从某个
#    Node 体冒泡到这一层（今天的六层函数不会——DraftContent/ProposedEdge
#    等模型的构造都在各自模块内部完成并被其 PROGRAMMING_ERRORS/Exception
#    分层捕获），也会被归类为"不重试"。我认为这是可接受的：schema 校验
#    失败通常是数据形状问题，同一份输入重试三次不会自愈。
# 2. DeepSeekExtractor/_DeepSeekVoter 在模型没调用强制工具时会
#    `raise ValueError("模型未调用 XXX 工具...")`——这是一种理论上"重试
#    也许换个结果"的情形。但今天这个 ValueError 总是先被 extract_all /
#    review_drafts / review_edges 各自的逐条 try/except 吞掉、转成
#    DropRecord，不会以裸 ValueError 的身份冒泡到 Node 体外层、也就摸不到
#    这条 retry_on 排除规则。也就是说，当前代码路径下这条代价是"理论上
#    存在、实际不会触发"；如果未来六层函数的 catch 边界发生变化，这一点
#    需要重新评估。
NODE_RETRY_EXCLUDED_ERRORS = PROGRAMMING_ERRORS + (ValueError,)


def retry_on(exc: Exception) -> bool:
    return not isinstance(exc, NODE_RETRY_EXCLUDED_ERRORS)


RETRY_POLICY = RetryPolicy(max_attempts=3, retry_on=retry_on)
# **I1：Node 级重试的真实语义（Task 5 code review 要求补记，均已实测）**——
# 只看 `build_graph()` 里四个 Node 各挂了 `retry_policy=RETRY_POLICY` 会
# 得出错误结论，这里把两条实测结果写清楚：
#
# 1. **对"LLM 调用偶发失败"这一类真实故障不可达**：extract/dedupe/edges/
#    review 四层背后的六层纯函数（extract_all/dedupe/propose_all/
#    review_drafts/review_edges）一致采用「逐条 try/except：
#    `PROGRAMMING_ERRORS` 直接 raise，其余 Exception 转成 DropRecord 并
#    continue，不冒泡出该层函数本身」的策略（"单条目失败不中断整批"）。
#    这意味着 API 限流/超时/网络抖动这类典型 LLM 故障，从不会让 Node 抛出
#    异常，`RetryPolicy` 也就从未被触发。实测：向 extractor 注入 API 类
#    异常，`extractor` 调用计数仍等于 chunk 数（每条各调一次，没有一次
#    重试）。
# 2. **真能触发时，粒度是整层——比手写版粗**：只有当六层函数本身整体抛出
#    （例如磁盘写入失败、六层函数自身的编程错误之外的意外故障）时，
#    `RetryPolicy` 才会重跑，但重跑单位是"整个 Node 再调一次"，也就是把
#    这一层的全部条目重新处理一遍，不管之前哪些条目本来是成功的。实测：
#    4 个 chunk、故障注入在 extract 层处理完最后一个 chunk 时抛出 → 整层
#    实际被调用 8 次（无故障场景是 4 次），即前 3 个已成功的 chunk 也被
#    重新跑了一遍。
#
# **结论（这是这次 A/B 对比的一手素材，不只是活在报告里）**：手写版逐条
# try/except 的重试粒度——只重试失败的那一条——在这个维度上严格优于
# LangGraph 的 Node 级 RetryPolicy；框架这层"重试能力"的真实价值仅限于
# "整层调用彻底失败"这一较窄的故障类别，覆盖不到"LLM 单次调用失败"这个
# 最常见的真实故障场景（那类故障早被六层函数自己的逐条 try/except 吞掉、
# 转成 DropRecord 了）。

# 单个 LLM 层最长容忍时间。手写版**根本没有超时概念**，这条能力确实是框架
# 提供的——但不是"白送"，代价在下面写清楚。
#
# **C1 修复记录（Task 5 code review 打回）**：langgraph 的每 Node timeout 靠
# `pregel/_retry.py::_arun_with_timeout` 让 Node 的后台 task 和一个看门狗
# task 用 `asyncio.wait(..., FIRST_COMPLETED)` 赛跑。这要求 Node 的协程体在
# 阻塞期间必须真正把控制权交还给事件循环（即内部要有 `await`），看门狗才有
# 机会被调度。四个 LLM Node 曾经是 `async def` 但函数体从不 `await`（直接
# 同步调用六层函数，底下是阻塞的 anthropic SDK HTTP 请求）——这会把事件
# 循环整个占住，看门狗拿不到调度机会，等到阻塞调用自己返回时，
# `_arun_with_timeout` 里 `if bg in done` 这一步会先于超时分支判定"任务按时
# 完成"，`timeout` 参数形同虚设。已用真实 HTTP 阻塞函数体 + 1 秒 timeout
# 实测复现：正常返回，不超时（对照组：函数体里有真 `await` 时，超时如期
# 触发，`NodeTimeoutError ... exceeded its run timeout of 1.000s`）。
#
# 修复：四个 Node 内部对六层函数的调用改成 `await asyncio.to_thread(...)`，
# 把阻塞调用扔进线程池，事件循环空出来才能真正调度看门狗。
#
# **代价（第一部分遗留项回归修正——这句话曾经写错，已用实测推翻）**：
# 早先这里写的是"这意味着'流水线不再永远挂着'，但不意味着底层资源被真正
# 取消"——前半句不成立，已用真实阻塞函数体 + 1 秒 timeout 实测推翻（见
# `test_node_timeout_does_not_provide_a_wall_clock_bound`）：sleep=3s、
# NODE_TIMEOUT=1s，`NodeTimeoutError` 如期在约 1s 抛出，但
# `run_pipeline_lg` **实际返回耗时接近 3s**，不是 1s。
#
# 根因在 CPython 的 `asyncio.runners.Runner.close()`：它会
# `run_until_complete(loop.shutdown_default_executor(THREAD_JOIN_TIMEOUT))`
# （`THREAD_JOIN_TIMEOUT = 300`），而 `asyncio.to_thread` 用的正是 default
# executor——`asyncio.run` 收尾时会去 join 那条杀不掉的后台线程，最多等
# 300 秒。也就是说：**`timeout` 改变的是返回结果（拿到 `NodeTimeoutError`
# 而不是正常返回值），不提供墙钟上界**。`run_pipeline_lg` 仍会阻塞到
# 底层调用自然结束、或 `asyncio.run` 的 300s executor join 上限——两者
# 谁先到就到谁，不是 `NODE_TIMEOUT` 那个数字。真实场景下（例如 10 分钟
# timeout + 一个卡死的 HTTP 请求），`run_pipeline_lg` 会在超时后再阻塞
# 最多 5 分钟才真正返回。
NODE_TIMEOUT = timedelta(minutes=10)


class PipelineState(TypedDict, total=False):
    """drops 是唯一带 reducer 的字段：手写版有五处显式 io.append_drops，
    这里声明一次，reducer 就把"跨层累加"这件事白送了，累加语义成了类型的
    一部分，state 里的 `drops` 字段和最终返回值天然是完整聚合结果。

    但 reducer 只解决"聚合到哪"，不解决"什么时候落盘"——它只在 state
    这个内存结构里累加，state 什么时候被落到磁盘上，reducer 完全不管。
    早期实现图省事，把 io.append_drops 放在 run_pipeline_lg 末尾、
    graph.invoke 返回之后调一次：这在 happy path 下和手写版行为一致，
    但一旦中间某个 Node 抛异常，graph.invoke 直接向上抛、永远走不到那次
    末尾调用，dropped.json 就完全不会被创建 —— 哪怕前面的层已经产生过
    丢弃记录。手写版不会有这个问题，因为它是每层跑完立刻
    `io.append_drops`，写盘动作和"这层跑完了"绑在一起，不依赖"整条流水线
    跑到底"这个前提。

    修复：每个 Node 在返回 delta 之前，自己先把这一层产生的 drops 落盘
    （`io.append_drops`），时序上与手写版对齐；随后仍在 delta 里把 drops
    交给 reducer，供最终返回值和跨层守恒断言使用。这意味着 run_pipeline_lg
    末尾**不能**再调用一次 io.append_drops——否则每条记录会被写盘两次。
    也就是说，reducer 白送了"聚合"这个语义，但没有白送"每层跑完立刻
    持久化"这个时序保证——那部分依然得自己写，一行都不能省。这是这次
    A/B 对比里一条真实的取舍记录，不是"框架都帮你做好了"。
    """

    source_dir: str
    out_dir: str
    model_id: str
    curriculum: str

    chunks: list[Chunk]
    # **B 阶段追加的 reducer**：drafts 原本是覆盖语义（A 阶段 node_extract 是
    # 唯一写它的 Node，覆盖等价于累加，改动不影响 A 阶段行为——已用全量测试
    # 实测确认，见 graph_fanout.py 顶部的任务报告）。B 阶段把 extract 扇出到
    # 条目级后，多个并发 Send 任务会在同一个 superstep 里各自写一份 drafts
    # delta；LangGraph 要求同一 step 内对同一 channel 的多次写入必须有 reducer
    # 才能合并，否则会抛 InvalidUpdateError。operator.add 就是这里需要的
    # "多份 delta 拼接成一份完整列表"语义。
    drafts: Annotated[list[TopicDraft], operator.add]
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
    # 立刻落盘，不等 run_pipeline_lg 末尾统一处理 —— 否则后面某层崩溃时
    # 这条记录会随进程一起消失，见 PipelineState 文档
    io.append_drops(_out(state) / "dropped.json", drops)
    return {"chunks": chunks, "drops": drops}


async def node_extract(state: PipelineState, runtime) -> dict:
    """async 本身是编译期硬要求（LangGraph 1.2.9 的 per-node timeout 只支持
    async node，见 langgraph.pregel._utils.validate_timeout_supported），
    但光有 `async def` 不够——见 NODE_TIMEOUT 定义处的 C1 修复记录：
    `await asyncio.to_thread(...)` 才是让 NODE_TIMEOUT 真正生效的那一步，
    不是可选的风格选择。函数体仍是「取 state → 调那层已有同步纯函数 →
    返回 delta」，没有多写一行业务逻辑，只是把"调用方式"从直接同步调用
    换成"扔进线程池、await 它的结果"。
    """
    drafts, drops = await asyncio.to_thread(
        extract_mod.extract_all, state["chunks"], runtime.context.extractor
    )
    io.write_stage(_out(state) / "02-drafts.json", drafts)
    io.append_drops(_out(state) / "dropped.json", drops)
    return {"drafts": drafts, "drops": drops}


async def node_dedupe(state: PipelineState, runtime) -> dict:
    # 见 node_extract 的注释与 NODE_TIMEOUT 定义处的 C1 修复记录：
    # await asyncio.to_thread 是让 NODE_TIMEOUT 真正生效的必要条件。
    result = await asyncio.to_thread(
        dedupe_mod.dedupe, state["drafts"], runtime.context.same_topic_judge
    )
    io.write_stage(_out(state) / "03-deduped.json", result.kept)
    io.write_stage(_out(state) / "merges.json", result.merges)
    io.append_drops(_out(state) / "dropped.json", result.drops)
    return {"deduped": result.kept, "merges": result.merges, "drops": result.drops}


async def node_edges(state: PipelineState, runtime) -> dict:
    # 见 node_extract 的注释与 NODE_TIMEOUT 定义处的 C1 修复记录。
    proposed, drops = await asyncio.to_thread(
        edges_mod.propose_all, state["deduped"], runtime.context.edge_proposer
    )
    io.write_stage(
        _out(state) / "04-edges.json",
        [
            TargetedEdge(target_draft_id=target, edge=e)
            for target, group in proposed.items()
            for e in group
        ],
    )
    io.append_drops(_out(state) / "dropped.json", drops)
    return {"proposed": proposed, "drops": drops}


async def node_review(state: PipelineState, runtime) -> dict:
    """本层唯一调 LLM 的两步（review_drafts / review_edges）用
    `await asyncio.to_thread(...)` 包起来——见 NODE_TIMEOUT 定义处的 C1
    修复记录。中间的 filter_edges_by_kept_drafts / detect_orphans 是纯
    Python 集合运算，不碰网络，没有阻塞事件循环的必要，保持直接同步调用。

    **S2 修复：append_drops 按手写版的粒度、算出来就立刻落盘，而不是攒到
    函数末尾一次性 append**。早期实现把四段 drops（draft_review.drops /
    prefilter_drops / edge_review.drops / orphan_drops）攒起来，在函数最后
    合并成一次 `io.append_drops` 调用；这与手写版 `run_pipeline` 的四次独立
    `io.append_drops`（每算出一段就立刻写）在"零故障"时最终文件内容相同，
    但一旦 `review_edges` 在中途抛出异常（Node 整个失败、从未走到函数末尾
    那次合并 append），此前已经算出的 `draft_review.drops` /
    `prefilter_drops` 会随进程一起丢失——而手写版此时已经把它们写盘了。
    已实测复现（见 test_run.py 的
    `test_two_engines_produce_identical_dropped_json_when_review_edges_crashes`
    等三个场景，RED 时 LangGraph 版 dropped.json 缺失或残缺，见
    task-5-report.md）。

    修复：拆回四次独立 `io.append_drops`，顺序与手写版一一对应
    （draft_review.drops → prefilter_drops → edge_review.drops →
    orphan_drops）。checkpoint 重跑时 Node 若整体重新执行，这四次 append
    会把同样的记录再算一遍、再写一遍——靠 `io.append_drops` 新增的幂等
    （按 `(stage, ref, reason, detail)` 去重，见 io.py 的 docstring）兜住，
    不会产生重复记录。
    """
    deps = runtime.context
    drops_path = _out(state) / "dropped.json"

    draft_review = await asyncio.to_thread(
        review_mod.review_drafts, state["deduped"], deps.fidelity_judges, deps.name_judges
    )
    io.append_drops(drops_path, draft_review.drops)

    kept_ids = {d.draft_id for d in draft_review.kept}
    surviving, prefilter_drops = review_mod.filter_edges_by_kept_drafts(
        state["proposed"], kept_ids
    )
    io.append_drops(drops_path, prefilter_drops)

    edge_review = await asyncio.to_thread(
        review_mod.review_edges,
        {d.draft_id: d for d in draft_review.kept},
        surviving,
        deps.edge_judges,
    )
    io.append_drops(drops_path, edge_review.drops)

    orphan_drops = review_mod.detect_orphans(
        draft_review.kept, state["proposed"], edge_review.kept_edges
    )
    io.append_drops(drops_path, orphan_drops)

    io.write_stage(_out(state) / "05-reviewed.json", draft_review.kept)
    io.write_stage(
        _out(state) / "review-log.json", draft_review.outcomes + edge_review.outcomes
    )
    review_drops = draft_review.drops + prefilter_drops + edge_review.drops + orphan_drops
    return {
        "reviewed": draft_review.kept,
        "kept_edges": edge_review.kept_edges,
        "outcomes": draft_review.outcomes + edge_review.outcomes,
        "drops": review_drops,
    }


def node_assemble(state: PipelineState, runtime) -> dict:
    """**S2 修复（推翻此前"挪到函数末尾"的方案，改回手写版同序）**：早期
    实现刻意把 `dup_drops` 的 `io.append_drops` 挪到函数最末尾（"整个 Node
    成功后才写盘"），理由是 assemble 没有 `retry_policy`，checkpoint 重跑
    会让函数体整个重新执行，若在 `assemble_mod.assemble`/`run_all` 之前就
    落盘，重跑会让 `dup_drops` 重复 append。

    但这个方案本身制造了一个新问题，且更严重：`run_all` 校验本身抛出异常
    时（例如校验器 bug），`dup_drops` 从未被落盘，而手写版 `run_pipeline`
    是在调用 `assemble`/`run_all` **之前**就把 `duplicate_edge_drops`
    append 了——两个引擎在"崩溃现场"的 dropped.json 从此不再对等（已实测
    复现：`test_two_engines_produce_identical_dropped_json_when_run_all_
    crashes_after_duplicate_edge`，RED 时 LangGraph 版缺失 DUPLICATE_EDGE
    记录）。这违反本分支的核心前提——两个引擎只在编排机制上不同，不在
    可观察产物上不同。

    **现在的修法**：把 `io.append_drops(dup_drops)` 挪回
    `dedupe_edges_by_pair` 算出来之后立刻执行，与手写版顺序一致；
    checkpoint 重跑导致的重复 append 交给 `io.append_drops` 新增的幂等
    （按 `(stage, ref, reason, detail)` 去重，见 io.py 的 docstring）兜住，
    而不是靠"延后写盘的时机"人工避免重复。
    `test_assemble_retry_via_checkpoint_does_not_duplicate_drops` 仍然
    覆盖"checkpoint 重跑不产生重复记录"这条要求，只是现在由幂等而非时机
    来满足。

    **`graph.json` 的写入时机不变**：它排在 `run_all` 之前，`run_all`
    之后仍可能抛异常，届时 `graph.json` 已经落盘但 Node 整体判定为失败。
    这不算危险——`write_text` 是**覆盖型**写入，天然幂等，不管重跑多少次
    磁盘上最终只有一份内容，不像 append 型写入那样"重复的尝试 = 重复的
    记录"。挪到函数末尾的代价是：若 `run_all` 抛异常，会连带丢失本次已经
    正确装配出来的 `graph.json`，对定位"是校验器崩了还是图本身有问题"
    反而更不利，所以维持原样。

    **无论哪种写入方式都盖不住的一个洞**：Node 函数体跑完（包括这里的
    `io.append_drops`）到 LangGraph 把这次执行结果落进 checkpoint 数据库
    之间，如果进程在这个窗口被硬杀（SIGKILL 等，而非 Python 异常），
    LangGraph 不会认为这次执行已完成，下一次 resume 会把整个 Node 从头
    重新跑一遍——`dup_drops` 会被重新算出并再 append 一次；这次不会产生
    重复记录（幂等兜底），但如果那个更窄的时间窗口里 checkpoint 落盘发生
    在两次 append 之间的某个更细粒度切面上，仍然只有"进程本身抛出的
    Python 异常"这一类失败被这次修复覆盖到，SIGKILL 类失败不在讨论范围内。
    """
    drops_path = _out(state) / "dropped.json"
    deduped_edges, dup_drops = assemble_mod.dedupe_edges_by_pair(state["kept_edges"])
    io.append_drops(drops_path, dup_drops)

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
    g.add_node("extract", node_extract, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("dedupe", node_dedupe, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("edges", node_edges, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
    g.add_node("review", node_review, retry_policy=RETRY_POLICY, timeout=NODE_TIMEOUT)
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
    checkpoint_db: Path | None = None,
    thread_id: str = "default",
) -> list[Finding]:
    """与 run.run_pipeline 行为对等的 LangGraph 版入口。

    `checkpoint_db` 为 None 时行为与 Task 4 一致（不带 checkpointer，每次
    从头跑）。传入路径后，同一 `thread_id` 第二次调用会从上次中断处续跑，
    不重跑已完成的 Node —— 手写版没有这个能力。

    **为什么内部用 `asyncio.run(...ainvoke(...))` 而不是 brief 原文的
    `.invoke(...)`**（已实测，非揣测）：langgraph 1.2.9 的每 Node
    `timeout` 只在异步执行路径上受支持。`.invoke()` 落到的同步执行器
    `pregel/_retry.py::run_with_retry` 只要 `task.timeout is not None`
    就无条件 `raise sync_timeout_unsupported`——即便 Node 函数本身已经是
    `async def`（本文件四个走 LLM 的 Node 均已如此，见 node_extract 的
    注释）。只有 `.ainvoke()` 落到的 `arun_with_retry` 才会真正遵守
    timeout。`run_pipeline_lg` 对外仍是同步函数（CLI/测试都同步调用），
    所以在函数体内用 `asyncio.run` 包一层，不改变对外签名。

    注意：这里**不**再调用一次 `io.append_drops(result["drops"])`。每个
    Node 已经在自己返回 delta 之前把这一层产生的 drops 落盘过一次（见
    PipelineState 文档的取舍说明）；`result["drops"]` 只是 reducer 聚合出的
    完整列表，用于返回值。若在这里再 append 一次，dropped.json 里每条记录
    都会重复一份 —— 这正是本任务要处理的交互风险之一，且与是否发生过重试
    / 续跑无关：哪怕全程零故障，这一条额外 append 也会让每条记录翻倍。

    **I3：调用方约束（Task 5 code review 要求补记）**——本函数内部用
    `asyncio.run(...)` 包住 `.ainvoke(...)`，这要求调用时**当前线程没有
    正在运行的 event loop**。若从一个已有 event loop 的上下文（例如某个
    `async def` 函数内部，或已经跑起来的 asyncio server）直接调用
    `run_pipeline_lg`，会得到
    `RuntimeError: asyncio.run() cannot be called from a running event loop`。
    本项目当前的 CLI 入口和 pytest 用例都是同步调用，不会撞上这条限制；
    但模块顶部文档已经预告了后续的 `graph_fanout.py`，任何把这条流水线
    包进已有 async 上下文（包括未来可能的 server 化）的用法都需要注意这一点
    —— 届时应改为直接 `await` 内部的 `_ainvoke()` 逻辑，而不是再包一层
    `asyncio.run`。

    **S1 修复（Critical）**：resume 分支此前传 `None` 给 `ainvoke`，这意味着
    本次调用的 `source_dir`/`out_dir`/`model_id`/`curriculum`（连同 CLI 的
    `--source`/`--out`）被整个丢弃、且没有任何一致性校验——只要两次调用
    复用同一个 `(checkpoint_db, thread_id)`，第二次调用不管传了什么参数，
    实际跑的都是 checkpoint 里存的第一次那一份，产物落进第一次的 `out_dir`，
    第二次自己的 `out_dir` 全程不会被写入任何东西，却能拿到退出码 0（已用
    `test_resume_with_different_source_or_out_raises_instead_of_silently_
    reusing_old_dirs` 实测复现）。这在下一个任务——多组受控实验——里是
    致命的：只要实验脚本复用一个 checkpoint 路径，各组会互相污染且不报错。

    修复：resume 前用 `_ensure_consistent_resume` 校验 checkpoint 里存的
    四个字段与本次调用参数是否一致，不一致就 raise ValueError，明确指出
    冲突字段和两边各自的值，绝不静默沿用旧参数。
    """
    out_dir.mkdir(parents=True, exist_ok=True)
    if checkpoint_db is not None:
        # 父目录不存在时 sqlite 只会抛裸 OperationalError（"unable to open
        # database file"），排查体验很差——这里同 out_dir 一样提前建好。
        checkpoint_db.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "source_dir": str(source_dir),
        "out_dir": str(out_dir),
        "model_id": model_id,
        "curriculum": curriculum,
        "drops": [],
    }
    config = {"configurable": {"thread_id": thread_id}}

    async def _ainvoke() -> dict:
        if checkpoint_db is None:
            app = build_graph().compile()
            return await app.ainvoke(payload, config=config, context=deps)
        async with AsyncSqliteSaver.from_conn_string(str(checkpoint_db)) as saver:
            app = build_graph().compile(checkpointer=saver)
            # 续跑：同一 thread_id 已有 checkpoint 时传 None，
            # LangGraph 会从上次中断处继续，而不是从头再来
            existing = await app.aget_state(config)
            if existing.next != ():
                _ensure_consistent_resume(existing.values, payload, thread_id=thread_id)
                return await app.ainvoke(None, config=config, context=deps)
            if existing.values:
                # **Critical 1 修复**：`existing.next == ()` 且 `existing.values`
                # 非空，说明这个 thread 之前已经跑到底了——这正是
                # `run.py::derive_thread_id` 刻意设计出的生产 CLI 默认路径
                # （同一实验、同一 --source/--out 重跑派生出同一个
                # thread_id）。此前这条分支直接 `ainvoke(payload, ...)`：
                # LangGraph 在同一 thread 上开新 run **不会重置 channel**，
                # 而 `drafts`/`drops` 都带 `operator.add` reducer——第二次的
                # delta 会叠加在第一次已完成的值上，dedupe 因此看到两倍同名
                # 草稿、互相当成重名抵消，产出一份 topics 归零的 graph.json
                # （实测复现见 test_rerunning_a_completed_thread_does_not_
                # accumulate_state，RED 时 topics=0）。
                #
                # 修法（选 (a)+(c) 的组合，理由见 task-8-report.md）：
                # 1. 复用 `_ensure_consistent_resume` 校验本次参数与 checkpoint
                #    里存的是否一致——这同时补上一个残余洞：此前该函数只在
                #    `existing.next != ()`（未跑完）分支被调用，同一
                #    `--thread-id` 配完全不同的 `--out`、且上一次已经跑完，
                #    是不会被拦截的（实测复现见
                #    test_rerunning_a_completed_thread_with_different_args_
                #    raises，RED 时 DID NOT RAISE）。
                # 2. 校验通过（确实是同一实验的重跑）后，显式
                #    `saver.adelete_thread(thread_id)` 清空这个 thread 的全部
                #    checkpoint，再 `ainvoke(payload, ...)`——channel 回到
                #    "从未跑过"的状态，reducer 不会看到上一轮的旧值。
                #    没有选纯粹的"跑完就 raise，逼用户换 thread_id"（选项
                #    a）：那会让"改了源文件、用同一个 --checkpoint 重新生成"
                #    这个 derive_thread_id 自己承诺过的默认路径也一并报错，
                #    对生产 CLI 是不必要的倒退。
                _ensure_consistent_resume(existing.values, payload, thread_id=thread_id)
                await saver.adelete_thread(thread_id)
            return await app.ainvoke(payload, config=config, context=deps)

    result = asyncio.run(_ainvoke())
    return result["findings"]


# S1 修复：resume 前校验的字段——这四个字段一旦跟 checkpoint 里存的不一致，
# 说明调用方其实是想跑一次不同的实验，而不是续跑同一次。
_RESUME_CONSISTENCY_FIELDS = ("source_dir", "out_dir", "model_id", "curriculum")


def _ensure_consistent_resume(stored: dict, payload: dict, *, thread_id: str) -> None:
    """resume 前的一致性校验：checkpoint 里存的字段必须与本次调用参数一致。

    不一致就 raise，绝不静默沿用 checkpoint 里的旧值——静默沿用正是 S1
    Critical 的根源：`thread_id` 相同、`checkpoint_db` 相同，但
    `source_dir`/`out_dir` 完全不同的两次调用，此前会被当成"同一次实验的
    续跑"处理，第二次调用的参数被整个丢弃。
    """
    mismatches = [
        (field, stored.get(field), payload[field])
        for field in _RESUME_CONSISTENCY_FIELDS
        if stored.get(field) != payload[field]
    ]
    if not mismatches:
        return
    detail = "；".join(
        f"{field}: checkpoint 里存的是 {stored_value!r}，本次调用传的是 {payload_value!r}"
        for field, stored_value, payload_value in mismatches
    )
    raise ValueError(
        f"checkpoint（thread_id={thread_id!r}）与本次调用参数不一致，"
        f"拒绝静默沿用旧参数续跑——{detail}。"
        "如果确实想跑一次不同的实验，请换一个 thread_id 或 checkpoint_db；"
        "如果只是想续跑同一次实验，请确认 --source/--out 与上次一致。"
    )
