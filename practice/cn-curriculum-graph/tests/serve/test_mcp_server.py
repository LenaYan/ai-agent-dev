"""绑定层：只测"接线"。

不重复测领域层已经测过的行为（`test_query.py` 51 条已经把检索、
排序、known_ids 语义、环、空图全测过了）。这里只回答四个问题：
六个工具都注册了吗、input schema 与领域层签名一致吗、参数原样转发了吗、
返回体能被 pydantic 反解吗。

**这层测试的寿命预期很短**：MCP SDK v2 会把 FastMCP 更名 MCPServer、
协议从有状态双向改无状态请求/响应。到那天领域层与 `test_query.py`
一行不动，要重写的正是这个文件 —— 这就是分层的收益，也是这层
刻意写薄的原因。
"""

import asyncio
import json

import pytest

from cn_curriculum_graph.models import Misconception
from cn_curriculum_graph.serve.mcp_server import TOOL_NAMES, build_server
from cn_curriculum_graph.serve.query import GraphIndex, PlanResult, TopicDetail
from conftest import dep, graph, topic


def _server():
    index = GraphIndex(
        graph(
            topics=[
                topic("int_add", name="整数加法", grade_start=1),
                topic("dec_add", name="小数加法", grade_start=4),
                topic(
                    "frac_add",
                    name="分数加法",
                    grade_start=5,
                    misconceptions=[
                        Misconception(
                            statement="「1/2 + 1/3 = 2/5，分子分母各自相加」",
                            probe="半块饼加三分之一块饼，有没有可能只剩五分之二？",
                            correction_hint="先通分：把两块饼切成同样大小的份。",
                        )
                    ],
                ),
            ],
            dependencies=[dep("frac_add", "dec_add"), dep("dec_add", "int_add")],
        )
    )
    return build_server(index)


def _call(name: str, arguments: dict):
    """调用工具并取回结构化返回体。

    FastMCP 的 `call_tool(convert_result=True)` 同时给出人读的 content 块
    与结构化 dict；测试只关心后者 —— 前者是 SDK 的序列化细节，不是我们的接线。
    """
    result = asyncio.run(_server().call_tool(name, arguments))
    if isinstance(result, tuple):
        _, structured = result
        return structured
    return result


def test_all_six_tools_are_registered():
    tools = asyncio.run(_server().list_tools())

    assert {t.name for t in tools} == set(TOOL_NAMES)
    assert len(TOOL_NAMES) == 6


def test_every_tool_ships_a_description():
    """description 是 agent 决定"要不要调这个工具"的唯一依据，缺一个就等于没上线。"""
    tools = asyncio.run(_server().list_tools())

    assert all(t.description for t in tools)


def test_input_schemas_expose_the_documented_parameters():
    schemas = {t.name: t.inputSchema["properties"] for t in asyncio.run(_server().list_tools())}

    assert set(schemas["search_topics"]) == {"query", "grade", "limit"}
    assert set(schemas["match_misconceptions"]) == {"observation", "limit"}
    assert set(schemas["get_topic"]) == {"topic_id"}
    assert set(schemas["get_prerequisites"]) == {"topic_id", "depth", "include_soft"}
    assert set(schemas["plan_path"]) == {"target_id", "known_ids", "include_soft"}
    assert set(schemas["get_graph_stats"]) == set()


def test_search_topics_forwards_query_and_limit():
    hits = _call("search_topics", {"query": "加法", "limit": 1})["result"]

    assert len(hits) == 1


def test_get_topic_returns_a_body_the_domain_model_can_reparse():
    body = _call("get_topic", {"topic_id": "frac_add"})

    detail = TopicDetail.model_validate(body)
    assert detail.provenance.review_status == "unreviewed"
    assert detail.misconceptions[0].probe.startswith("半块饼")


def test_plan_path_forwards_known_ids_verbatim():
    """known_ids 是最容易在转发层被改写语义的参数（列表 vs 逗号串），单独钉一条。"""
    body = _call("plan_path", {"target_id": "frac_add", "known_ids": ["dec_add"]})

    plan = PlanResult.model_validate(body)
    assert [s.topic_id for s in plan.steps] == ["frac_add"]
    assert sorted(plan.skipped_known) == ["dec_add", "int_add"]


def test_get_graph_stats_takes_no_arguments():
    assert _call("get_graph_stats", {})["topic_count"] == 3


def test_unknown_topic_id_surfaces_as_a_tool_error():
    """领域层抛 TopicNotFoundError，绑定层要让它变成 MCP 的工具错误，
    而不是把 server 打挂或者静默返回空。"""
    with pytest.raises(Exception) as excinfo:
        _call("get_topic", {"topic_id": "NOPE"})

    assert "NOPE" in str(excinfo.value)


def test_tool_results_are_json_serializable():
    """跨进程传出去的是 JSON，本地能构造 pydantic 对象不代表它出得去。"""
    body = _call("get_prerequisites", {"topic_id": "frac_add", "depth": 2})

    assert json.loads(json.dumps(body))["result"][0]["reason"]
