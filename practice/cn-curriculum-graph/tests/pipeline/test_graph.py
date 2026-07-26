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
