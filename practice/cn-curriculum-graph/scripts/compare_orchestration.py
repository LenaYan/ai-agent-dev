"""受控实验：同一份素材、同样的故障，量两个编排实现（手写 vs LangGraph）的差异。

**这是 fake 实验，不是生产实测**：extractor/judges/proposer 全部替换成
确定性的假实现（见 `_fake_deps`），故障靠代码注入而非真实网络/API。
好处是可复现、零成本、故障位置精确可控；代价是测不出真实限流的行为
（真实 429 常伴随 `Retry-After` 头、并发退避、抖动等），也测不出真模型
输出内容的质量差异。**读这份数据表时不要当成生产环境的实测结论。**

**四种故障注入方式，刻意不同，都是被实测逼出来的取舍**：

1. `judge_exception_swallowed` 场景（此前叫 `rate_limit`，Task 6 code
   review 打回后改名——原名有限流语义误导性，`times=1` 只失败一次，没有
   `Retry-After`、没有退避、没有连续失败，测的根本不是限流）：故障挂在
   `PipelineDeps` 的字段上（`faults.wrap_deps`）。已实测（见
   `src/cn_curriculum_graph/pipeline/graph.py` 的 I1 注释）：extract/
   dedupe/edges/review 四层背后的六层纯函数一律「逐条 try/except，非
   程序错误转成 DropRecord、continue」，故障永远不会从这一层冒泡出去，
   Node 级 `RetryPolicy` 从不可达。所以这个场景**不会**让流水线崩溃，
   也就用不上"第二轮恢复重跑"——两个引擎都应该在第一轮就顺利结束，
   两边数字完全相同。这条结论不该跟"恢复重跑"对比表混排——那会被读成
   "两个引擎在限流下表现相同"，而实际是"这个注入方式压根没触发任何
   重试机制"，两者含义完全不同。见 `main()` 里的输出分段。
2. `hard_crash` 场景：如果把故障挂在 deps 上（例如 `edge_proposer`），
   会撞上同一条限制——`edges_mod.propose_all` 对每个 target 各自
   try/except，故障只会变成一条 `DropRecord`，永远不会让整层真正抛出。
   要让整层不可恢复地失败，必须直接顶替纯函数本身
   （`unittest.mock.patch.object`），绕开逐条 try/except，这样两个引擎
   才会真的各自体验一次"整层崩溃 -> 需要恢复"。**但这个注入方式在碰到
   `proposer` 之前就抛**（`_boom_propose_all`），意味着 LangGraph 的
   `RetryPolicy` 把 edges Node 重跑 3 次全部是零调用——重试的代价被
   完全隐藏，表里只记了 checkpoint 跳过 chunk/extract/dedupe 的收益，
   见 3 号场景的对照。
3. `partial_crash` 场景（Task 6 code review 修复 1 新增）：同样顶替
   `edges_mod.propose_all`，但先真实处理完"前一半有候选池的 target"
   （真的调用 `proposer`），再抛出（`_partial_boom_propose_all`）。
   这样 LangGraph 的每次重试尝试都会真实烧调用，重试的代价能在总调用数
   里如实体现，不会被 `hard_crash` 那种"整层立即抛"的注入方式白送掉。
   两个场景刻意并列呈现，作为"重试代价是否可见"的对照组。
4. `baseline` 场景：无故障，量正常路径的调用次数与耗时，作为其余场景的
   参照基线。

**规模参数**（Task 6 code review 修复 2）：`--chunks N` 控制合成源文本的
条目数（默认 3，与历史数据对齐）；知识点名从 `_TOPIC_POOL` 循环取值——
见该常量定义处的说明，模拟真实课标里核心概念跨年级复现的情形，让
`dedupe` 层的候选对数量随 chunk 数增长，而不是像"全不重名"那样恒为 0。
但池子里的名字彼此已用 `dedupe._containment_ratio` 逐对验证过相似度都
< 0.85 合并阈值——不是全部两两相似，避免虚抬候选对数（审查者自己合成
时踩过这个坑：近义词过多导致 P 明显偏大）。

**判定器数量**（Task 6 code review 修复 3）：`--judges-per-category`
默认 2，对齐 `run.py` CLI 的生产默认（`deepseek-v4-flash,deepseek-v4-pro`，
每类各挂 2 个投票者），而不是历史遗留的每类 1 个——1 个判定器时，恢复
重跑省下的调用数会被"下游判定器数量"稀释后的占比夸大。
"""

from __future__ import annotations

import argparse
import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from unittest.mock import patch

from cn_curriculum_graph.pipeline import edges as edges_mod
from cn_curriculum_graph.pipeline.dedupe import SameTopicVerdict
from cn_curriculum_graph.pipeline.faults import FaultSpec, wrap_deps
from cn_curriculum_graph.pipeline.graph import run_pipeline_lg
from cn_curriculum_graph.pipeline.models import (
    DraftBatch,
    DraftContent,
    ProposedEdge,
    ProposedEdgeBatch,
    Vote,
)
from cn_curriculum_graph.pipeline.run import PipelineDeps, run_pipeline
from cn_curriculum_graph.validators.consistency import Verdict

# 6 个互相区分度足够高的核心概念名（已逐对验证过 dedupe._containment_ratio
# < 0.85 合并阈值，见本文件对应的 Task 6 报告小节）。循环使用它们模拟真实
# 课标里"核心概念跨年级螺旋复现"的情形（如"分数的初步认识"从低年级到高
# 年级反复出现），让 dedupe 层的候选对数量随 chunk 数增长——但不是随手选
# 一堆近义词让相似度阈值虚高触发，那样会让 P 失真变大。
_TOPIC_POOL = (
    "分数的初步认识",
    "长方形与正方形",
    "简易方程的应用",
    "可能性的大小",
    "小数乘除运算",
    "图形的运动",
)


class JudgeException(Exception):
    """模拟判定器抛出的异常（此前用 `RateLimit` 命名，Task 6 修复 4 后改名
    ——它并不携带任何限流语义，`RateLimit` 这个名字本身就是误导）。"""


class HardCrash(Exception):
    """模拟不可恢复的中途崩溃（顶替纯函数本身，而不是挂在 deps 上）。"""


@dataclass(frozen=True)
class Scenario:
    """一个受控实验场景。

    `dep_specs`：走 `faults.wrap_deps` 挂在 deps 上的故障——已实测只会被
    六层函数的逐条 try/except 吞成 DropRecord，不会让 Node/整层失败。
    `crash_fn`：非 None 时，第一轮把 `edges_mod.propose_all` 整个顶替成
    这个函数，绕开逐条 try/except，制造一次真正的"整层不可恢复失败"；
    第二轮开始前撤销顶替，恢复原函数。
    `expected_exception`：第一轮"预期"抛出的异常类型；None 表示这个场景
    设计上不该崩溃。`run_scenario` 会拿它跟实际抛出的异常类型比对——不一致
    就说明抛出的不是本场景注入的故障，而是别的 bug，脚本会响亮地报错，
    不会把它悄悄计成"恢复重跑"数据（Task 6 code review 修复 5.1）。
    `in_main_table`：False 的场景不进"完成/总调用/恢复重跑"对比表，
    在 `main()` 里单独成段呈现（Task 6 code review 修复 4）。
    """

    name: str
    dep_specs: tuple[FaultSpec, ...]
    crash_fn: Any | None
    expected_exception: type[Exception] | None
    in_main_table: bool
    note: str


def _boom_propose_all(drafts: list, proposer: Any) -> tuple:
    """顶替 `edges_mod.propose_all` 本身，绕开它内部的逐条 try/except，
    让 edges 这一整层必定抛出、且抛得毫无自愈可能。**碰到 `proposer` 之前
    就抛**——重试的 3 次尝试全部是零调用，见模块文档第 2 点。"""
    raise HardCrash("模拟 edges 层持续故障，重试 3 次也救不回来")


def _partial_boom_propose_all(drafts: list, proposer: Any) -> tuple:
    """跑到一半才抛：先真实处理完"前一半有候选池的 target"（真的调用
    `proposer`），再抛出。和 `_boom_propose_all`（整层立即抛、零调用）
    对照，让重试的代价在总调用数里如实体现——不像 `hard_crash` 那样把
    重试的 3 次尝试全部白送成零成本（见模块文档第 3 点）。"""
    candidates = edges_mod.candidate_prerequisites(drafts)
    targets_with_pool = [t for t in drafts if candidates[t.draft_id]]
    half = len(targets_with_pool) // 2
    for target in targets_with_pool[:half]:
        proposer(target, candidates[target.draft_id])
    raise HardCrash("模拟 edges 层跑到一半才抛——前半段的调用已经真实发生，重试代价可见")


SCENARIOS = [
    Scenario(
        "baseline", (), None, None, True,
        "无故障，量正常路径的调用次数与耗时",
    ),
    Scenario(
        "judge_exception_swallowed",
        (FaultSpec(target="fidelity_judges", fail_on_call=1, exc=JudgeException, times=1),),
        None, None, False,
        "fidelity 判定第 1 次调用抛异常——按已实测结论（graph.py I1 注释），"
        "六层纯函数逐条 try/except 会把它吞成 DropRecord，Node 级 RetryPolicy "
        "从不可达，两个引擎都不应崩溃、不需要恢复重跑。注意：这里没有任何限流"
        "语义（times=1 只失败一次，没有 Retry-After、没有退避、没有连续失败），"
        "此前场景名叫 rate_limit 具有误导性，已改名（Task 6 code review 修复 4）",
    ),
    Scenario(
        "hard_crash", (), _boom_propose_all, HardCrash, True,
        "monkeypatch edges_mod.propose_all，让 edges 整层在碰到 proposer 之前"
        "就不可恢复地抛出——考察恢复时从哪里续跑；代价：重试的 3 次尝试全是"
        "零调用，重试代价被完全隐藏，见 partial_crash 对照",
    ),
    Scenario(
        "partial_crash", (), _partial_boom_propose_all, HardCrash, True,
        "monkeypatch edges_mod.propose_all，处理完前一半有候选池的 target 后"
        "才抛——每次重试尝试都真实烧调用，让重试代价在总调用数里如实体现，"
        "与 hard_crash（零成本重试）对照（Task 6 code review 修复 1）",
    ),
]


@dataclass
class ScenarioResult:
    engine: str
    scenario: str
    round1_crashed: bool
    crash_exception: str | None
    final_completed: bool
    total_calls: int
    recovery_calls: int
    seconds: float


def _build_fixture(n_chunks: int) -> tuple[str, dict[str, tuple[str, int]]]:
    """合成 n_chunks 条课标条目：标准编号 + 中文正文。

    知识点名从 `_TOPIC_POOL` 循环取值（见模块顶部注释），模拟真实课标里
    核心概念跨年级螺旋复现的情形，让 dedupe 层的候选对数量随 chunk 数
    增长。年级随 index 单调铺开到 1~6 年级。
    """
    if n_chunks < 1:
        raise ValueError(f"n_chunks 必须 >= 1，收到 {n_chunks!r}")

    lines: list[str] = []
    names: dict[str, tuple[str, int]] = {}
    for i in range(n_chunks):
        grade = 1 + min(5, (i * 6) // n_chunks)
        name = _TOPIC_POOL[i % len(_TOPIC_POOL)]
        code = f"{grade}.{i + 1}.1"
        names[code] = (name, grade)
        lines.append(f"{code} 能理解并运用「{name}」（第 {i + 1} 条目）。\n")
    return "\n".join(lines), names


def _make_extractor(names: dict[str, tuple[str, int]]):
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

    return extractor


def _fidelity_judge_factory(i: int):
    def judge(draft):
        return Vote(reviewer=f"fake-fidelity-{i}", approved=True, reason="ok")

    return judge


def _name_judge_factory(i: int):
    def judge(name, description):
        return Verdict(judgment="consistent")

    return judge


def _edge_judge_factory(i: int):
    def judge(target, edge):
        return Vote(reviewer=f"fake-edge-{i}", approved=True, reason="ok")

    return judge


def _fake_deps(names: dict[str, tuple[str, int]], judges_per_category: int = 2) -> PipelineDeps:
    """全 fake 的 deps。`judges_per_category` 默认 2，对齐生产 CLI 默认
    （见模块文档"判定器数量"一节）。"""

    def proposer(target, candidates):
        return ProposedEdgeBatch(
            edges=[
                ProposedEdge(prerequisite_draft_id=c.draft_id, strength="hard", reason="先学它")
                for c in candidates
            ]
        )

    return PipelineDeps(
        extractor=_make_extractor(names),
        same_topic_judge=lambda a, b: SameTopicVerdict(same=False, reason="不同"),
        edge_proposer=proposer,
        fidelity_judges=[_fidelity_judge_factory(i) for i in range(judges_per_category)],
        name_judges=[_name_judge_factory(i) for i in range(judges_per_category)],
        edge_judges=[_edge_judge_factory(i) for i in range(judges_per_category)],
    )


def _invoke(engine: str, source: Path, out: Path, deps: PipelineDeps, db: Path) -> None:
    if engine == "handwritten":
        # 手写版没有重入能力：第二轮必然从头跑，这正是要量的差异
        run_pipeline(source, out, deps, model_id="fake", curriculum="c")
    else:
        run_pipeline_lg(
            source, out, deps, model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="exp",
        )


def run_scenario(
    engine: str,
    scenario: Scenario,
    root: Path,
    *,
    chunks: int = 3,
    judges_per_category: int = 2,
) -> ScenarioResult:
    """跑一个场景。有故障时跑两轮（第一轮撞故障，第二轮恢复），
    recovery_calls 记的是第二轮的调用次数 —— 那就是"重复烧掉的量"。"""
    if root.exists():
        shutil.rmtree(root)
    source_text, names = _build_fixture(chunks)
    source = root / "source"
    source.mkdir(parents=True)
    (source / "m.md").write_text(source_text, encoding="utf-8")
    out = root / "out"
    db = root / "cp.sqlite"

    started = time.monotonic()
    first_deps, first_counter = wrap_deps(
        _fake_deps(names, judges_per_category), list(scenario.dep_specs)
    )
    round1_crashed = False
    crash_exception: str | None = None
    try:
        if scenario.crash_fn is not None:
            with patch.object(edges_mod, "propose_all", scenario.crash_fn):
                _invoke(engine, source, out, first_deps, db)
        else:
            _invoke(engine, source, out, first_deps, db)
    except Exception as exc:  # noqa: BLE001 —— 判完类型再决定要不要继续往外抛
        round1_crashed = True
        crash_exception = type(exc).__name__
        # Task 6 code review 修复 5.1：不能笼统地把 completed 设成 False 就完事——
        # 必须核实这次崩溃就是本场景设计要注入的那个异常类型，而不是别的 bug。
        if scenario.expected_exception is None:
            raise AssertionError(
                f"场景 {scenario.name!r} 没有设计任何崩溃（expected_exception=None），"
                f"但第一轮实际抛出了 {crash_exception}: {exc}——这很可能是别的 bug，"
                "不是本场景注入的故障，脚本拒绝把它悄悄计成'恢复重跑'数据"
            ) from exc
        if not isinstance(exc, scenario.expected_exception):
            raise AssertionError(
                f"场景 {scenario.name!r} 预期第一轮抛出 "
                f"{scenario.expected_exception.__name__}，实际抛出了 "
                f"{crash_exception}: {exc}——这不是本场景注入的故障，可能是别的 bug，"
                "脚本拒绝把它悄悄计成本场景的恢复重跑数据"
            ) from exc
    first_calls = sum(first_counter.counts.values())

    recovery_calls = 0
    if round1_crashed:
        # 第二轮：故障已消失（crash_fn 的顶替已经随 with 块退出被撤销），
        # 看各自要重跑多少
        second_deps, second_counter = wrap_deps(_fake_deps(names, judges_per_category), [])
        _invoke(engine, source, out, second_deps, db)
        recovery_calls = sum(second_counter.counts.values())

    return ScenarioResult(
        engine=engine,
        scenario=scenario.name,
        round1_crashed=round1_crashed,
        crash_exception=crash_exception,
        final_completed=True,
        total_calls=first_calls + recovery_calls,
        recovery_calls=recovery_calls,
        seconds=round(time.monotonic() - started, 2),
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="受控实验：量手写 vs LangGraph 编排的故障恢复差异（fake 实现，非生产实测）"
    )
    parser.add_argument(
        "--chunks", type=int, default=3,
        help="合成源文本的条目数（默认 3，与历史数据对齐；见模块文档'规模参数'一节）",
    )
    parser.add_argument(
        "--judges-per-category", type=int, default=2, dest="judges_per_category",
        help="每类判定器（fidelity/name/edge）挂几个 fake judge"
        "（默认 2，对齐 run.py CLI 的生产默认；见模块文档'判定器数量'一节）",
    )
    args = parser.parse_args(argv)

    root = Path("/tmp/ccg-compare")
    rows: list[tuple[Scenario, ScenarioResult]] = []
    for scenario in SCENARIOS:
        for engine in ("handwritten", "langgraph"):
            result = run_scenario(
                engine, scenario, root / f"{scenario.name}-{engine}",
                chunks=args.chunks, judges_per_category=args.judges_per_category,
            )
            rows.append((scenario, result))

    print(f"实验参数：chunks={args.chunks}，judges_per_category={args.judges_per_category}")
    print()

    main_rows = [(s, r) for s, r in rows if s.in_main_table]
    side_rows = [(s, r) for s, r in rows if not s.in_main_table]

    print("== 恢复重跑对比（有崩溃语义的场景）==")
    header = (
        f"{'场景':<18}{'引擎':<14}{'首轮崩溃':<10}{'崩溃异常':<14}"
        f"{'最终完成':<10}{'总调用':<8}{'恢复重跑':<10}耗时(s)"
    )
    print(header)
    print("-" * 88)
    for s, r in main_rows:
        print(
            f"{r.scenario:<18}{r.engine:<14}"
            f"{'是' if r.round1_crashed else '否':<10}"
            f"{r.crash_exception or '-':<14}"
            f"{'是' if r.final_completed else '否':<10}"
            f"{r.total_calls:<8}{r.recovery_calls:<10}{r.seconds}"
        )
    print()
    print(
        "注：fake 实现，非真实 API；恢复重跑 = 第一轮崩溃/故障消失后第二轮的调用次数；"
    )
    print(
        "耗时精度已降到 0.01s——含 RetryPolicy 退避的场景（hard_crash/partial_crash 的"
    )
    print(
        "langgraph 侧）实测同一配置重复运行会有明显 jitter 抖动（量级在 1~3s 之间浮动，"
    )
    print("不是稳定数字），只看量级，不要引用具体小数位。")
    print()

    print("== 单独成段：故障吞没边界（不是恢复重跑对比，Task 6 code review 修复 4）==")
    for s, r in side_rows:
        print(f"场景 {r.scenario}：两引擎的故障吞没边界一致，两边都没有发生重试。")
        print(
            f"  {r.engine:<14}首轮崩溃={'是' if r.round1_crashed else '否':<4} "
            f"总调用={r.total_calls} 恢复重跑={r.recovery_calls} 耗时(s)={r.seconds}"
        )
    print()

    print("场景说明：")
    for scenario in SCENARIOS:
        print(f"  {scenario.name}: {scenario.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
