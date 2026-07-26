"""受控实验：同一份素材、同样的故障，量两个编排实现（手写 vs LangGraph）的差异。

**这是 fake 实验，不是生产实测**：extractor/judges/proposer 全部替换成
确定性的假实现（见 `_fake_deps`），故障靠代码注入而非真实网络/API。
好处是可复现、零成本、故障位置精确可控；代价是测不出真实限流的行为
（真实 429 常伴随 `Retry-After` 头、并发退避、抖动等），也测不出真模型
输出内容的质量差异。**读这份数据表时不要当成生产环境的实测结论。**

**两种故障注入方式，刻意不同，都是被实测逼出来的取舍**：

1. `rate_limit` 场景：故障挂在 `PipelineDeps` 的字段上（`faults.wrap_deps`）。
   已实测（见 `src/cn_curriculum_graph/pipeline/graph.py` 的 I1 注释）：
   extract/dedupe/edges/review 四层背后的六层纯函数一律「逐条 try/except，
   非程序错误转成 DropRecord、continue」，故障永远不会从这一层冒泡出去，
   Node 级 `RetryPolicy` 从不可达。所以这个场景**不会**让流水线崩溃，
   也就用不上"第二轮恢复重跑"——两个引擎都应该在第一轮就顺利结束。
2. `hard_crash` 场景：如果还是把故障挂在 deps 上（例如 brief 最初设想的
   `FaultSpec(target="edge_proposer", ...)`），会撞上同一条限制——
   `edges_mod.propose_all` 对每个 target 各自 try/except，故障只会变成
   一条 `DropRecord`，永远不会让整层真正抛出。要让整层不可恢复地失败，
   必须直接顶替纯函数本身（`unittest.mock.patch.object`），绕开逐条
   try/except，这样两个引擎才会真的各自体验一次"整层崩溃 -> 需要恢复"。
"""

from __future__ import annotations

import shutil
import time
from dataclasses import dataclass
from pathlib import Path
from unittest.mock import patch

from cn_curriculum_graph.pipeline import edges as edges_mod
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
    """模拟不可恢复的中途崩溃（顶替纯函数本身，而不是挂在 deps 上）。"""


@dataclass(frozen=True)
class Scenario:
    """一个受控实验场景。

    `dep_specs`：走 `faults.wrap_deps` 挂在 deps 上的故障——已实测只会被
    六层函数的逐条 try/except 吞成 DropRecord，不会让 Node/整层失败。
    `layer_crash`：True 时，第一轮把 `edges_mod.propose_all` 整个顶替成
    必定抛 `HardCrash` 的假函数，绕开逐条 try/except，制造一次真正的
    "整层不可恢复失败"；第二轮开始前撤销顶替，恢复原函数。
    两者不会同时使用——当前三个场景里，只有其中一种非空。
    """

    name: str
    dep_specs: tuple[FaultSpec, ...]
    layer_crash: bool
    note: str


SCENARIOS = [
    Scenario(
        "baseline", (), False, "无故障，量正常路径的调用次数与耗时"
    ),
    Scenario(
        "rate_limit",
        (FaultSpec(target="fidelity_judges", fail_on_call=1, exc=RateLimit, times=1),),
        False,
        "fidelity 判定第 1 次调用抛 429——按已实测结论（graph.py I1 注释），"
        "六层纯函数逐条 try/except 会把它吞成 DropRecord，Node 级 RetryPolicy "
        "从不可达，两个引擎都不应崩溃、不需要恢复重跑",
    ),
    Scenario(
        "hard_crash",
        (),
        True,
        "monkeypatch edges_mod.propose_all，让 edges 整层不可恢复地抛出"
        "（挂在 deps.edge_proposer 上的 FaultSpec 会被逐条 try/except 吞掉，"
        "测不出'整层崩溃'，故改为直接顶替纯函数本身）——考察恢复时从哪里续跑",
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


def _boom_propose_all(drafts, proposer):
    """顶替 `edges_mod.propose_all` 本身，绕开它内部的逐条 try/except，
    让 edges 这一整层必定抛出、且抛得毫无自愈可能（不像挂在 deps 上的
    故障那样会被转成 DropRecord）。"""
    raise HardCrash("模拟 edges 层持续故障，重试 3 次也救不回来")


def _invoke(engine: str, source: Path, out: Path, deps, db: Path) -> None:
    if engine == "handwritten":
        # 手写版没有重入能力：第二轮必然从头跑，这正是要量的差异
        run_pipeline(source, out, deps, model_id="fake", curriculum="c")
    else:
        run_pipeline_lg(
            source, out, deps, model_id="fake", curriculum="c",
            checkpoint_db=db, thread_id="exp",
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
    first_deps, first_counter = wrap_deps(_fake_deps(), list(scenario.dep_specs))
    completed = True
    try:
        if scenario.layer_crash:
            # 顶替纯函数本身（而非挂在 deps 上），绕开逐条 try/except，
            # 让整层真的抛出——round 2 开始前会撤销这次顶替
            with patch.object(edges_mod, "propose_all", _boom_propose_all):
                _invoke(engine, source, out, first_deps, db)
        else:
            _invoke(engine, source, out, first_deps, db)
    except Exception:
        completed = False
    first_calls = sum(first_counter.counts.values())

    recovery_calls = 0
    if not completed:
        # 第二轮：故障已消失（layer_crash 的顶替已经随 with 块退出被撤销），
        # 看各自要重跑多少
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
    print("场景说明：")
    for scenario in SCENARIOS:
        print(f"  {scenario.name}: {scenario.note}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
