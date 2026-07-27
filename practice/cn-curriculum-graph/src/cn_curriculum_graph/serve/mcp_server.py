"""MCP 绑定层：六个 @tool，函数体只转发。

**这层刻意写薄**（论证见 docs/mcp-server-design.md §2）：MCP Python SDK
v2 会把 `FastMCP` 更名 `MCPServer`、协议从有状态双向改成无状态请求/响应，
v1.28.1 官方已标注维护模式。真到那天，要重写的只有这个文件 ——
`serve/query.py` 与它的 51 条测试一行不动。

工具的 description 不是文档，是**唯一的调用依据**：agent 只凭它决定要不要
调、传什么。所以每条都写清"什么时候用它"和"它不做什么"，
尤其要写明这张图未经教研审核 —— 诚实性设计不能在最后一米丢掉。
"""

from __future__ import annotations

import os
from pathlib import Path

from mcp.server.fastmcp import FastMCP

from cn_curriculum_graph.serve import query as q

TOOL_NAMES = (
    "match_misconceptions",
    "search_topics",
    "get_topic",
    "get_prerequisites",
    "plan_path",
    "get_graph_stats",
)

GRAPH_PATH_ENV = "CCG_GRAPH_PATH"

# 相对包位置解析，不相对 cwd —— **server 的 cwd 由 MCP 客户端决定**。
# 写成相对路径的话，"在我机器上能跑"会变成"在 Claude Code 里起不来"，
# 而症状只是一句 FileNotFoundError，看不出问题出在 cwd。
DEFAULT_GRAPH_PATH = (
    Path(__file__).resolve().parents[3] / "data" / "generated" / "graph.json"
)

INSTRUCTIONS = """中国小学数学课标知识依赖图（数与代数领域）。

两条使用线：
- 诊断：孩子答错/说错 → match_misconceptions 找到对应的误概念 → get_topic 取
  完整节点（evidence 是掌握判据、assessment_prompt 是可以直接问出口的一句话）。
- 规划：目标知识点 → plan_path 排学习序列（known_ids 传已掌握的），
  或 get_prerequisites 只看某个点的直接前置。

两件必须知道的事：
1. 这张图由 LLM 从课标原文抽取生成，**未经教研人员审核**（每个返回体的
   provenance.review_status 恒为 unreviewed、confidence 恒为 0.0）。
   转述给家长时要如实说明这一点。
2. 图很稀疏。开工前先调 get_graph_stats 看规模 —— 在一张孤儿遍地的图上
   做课程规划是自欺。检索是纯字面匹配，没命中就换个说法重试，
   返回空不等于"课标里没有"。"""


def build_server(index: q.GraphIndex) -> FastMCP:
    """把一个建好索引的图绑成 MCP server。

    接收 `GraphIndex` 而不是文件路径：图在进程启动时加载一次，
    工具调用路径上没有 I/O，测试也能直接注入 fixture 图。
    """
    server: FastMCP = FastMCP("cn-curriculum-graph", instructions=INSTRUCTIONS)

    @server.tool(
        description=(
            "把孩子的错误说法对应到课标知识点的典型误概念上。"
            "输入用孩子/家长的原话（如「0.45 比 0.405 小，因为 45 比 405 小」），"
            "返回 statement（这个错误长什么样）、probe（用来确认的提问）、"
            "correction_hint（往哪儿引）。诊断类问题优先用它，而不是 search_topics。"
            "纯字面匹配：没命中就换个说法再试一次。"
        )
    )
    def match_misconceptions(observation: str, limit: int = 5) -> list[q.MisconceptionHit]:
        return q.match_misconceptions(index, observation, limit=limit)

    @server.tool(
        description=(
            "按名称/描述检索知识点，返回精简卡片。"
            "grade 传 1-9 可只看覆盖该年级的节点（同名节点靠年级区分）。"
            "只给候选，不给完整内容 —— 拿到 id 后用 get_topic 取全文。"
        )
    )
    def search_topics(query: str, grade: int | None = None, limit: int = 5) -> list[q.TopicCard]:
        return q.search_topics(index, query, grade=grade, limit=limit)

    @server.tool(
        description=(
            "取知识点全文：evidence（掌握判据，可观察可验证）、"
            "assessment_prompt（一句话口头评测）、misconceptions、"
            "standards（课标条目编号）、provenance。id 不存在会报错。"
        )
    )
    def get_topic(topic_id: str) -> q.TopicDetail:
        return q.get_topic(index, topic_id)

    @server.tool(
        description=(
            "看某个知识点的前置，逐层往上。每条带 strength（hard=必须先会，"
            "soft=最好先会）与 reason（为什么是前置，可直接讲给家长）。"
            "depth 默认 2；include_soft=false 只看必须项。"
            "要完整学习序列用 plan_path，不要把这个工具的结果自己排序。"
        )
    )
    def get_prerequisites(
        topic_id: str, depth: int = 2, include_soft: bool = True
    ) -> list[q.PrerequisiteEdge]:
        return q.get_prerequisites(index, topic_id, depth=depth, include_soft=include_soft)

    @server.tool(
        description=(
            "排出学到目标知识点的完整学习序列（拓扑序，同层先低年级）。"
            "known_ids 传已经掌握的知识点 id —— 语义是「会了 X」蕴含「会了 X 的"
            "全部前置」，它们会连同上游一起从路径中剔除，剔掉了什么见 skipped_known。"
            "has_cycle 为真表示图里有环、这个顺序不可全信。"
            "每步的 revisits 说明这个概念在更高年级还会再来一轮。"
        )
    )
    def plan_path(
        target_id: str, known_ids: list[str] | None = None, include_soft: bool = True
    ) -> q.PlanResult:
        return q.plan_path(
            index, target_id, known_ids=known_ids or [], include_soft=include_soft
        )

    @server.tool(
        description=(
            "图的规模与形状：节点数、边数（hard/soft）、年级分布、覆盖的课标编号、"
            "孤立节点数、最长前置链、有没有环、审核状态分布。"
            "做课程规划之前先调它 —— 孤立节点多、最长链短的图上，"
            "任何「学习路径」都是退化的，这一点要如实告诉用户。"
        )
    )
    def get_graph_stats() -> q.GraphStats:
        return q.get_graph_stats(index)

    return server


def build_server_from_path(path: Path) -> FastMCP:
    return build_server(q.GraphIndex(q.load_graph(path)))


def main() -> None:
    """stdio 入口。图路径走环境变量 CCG_GRAPH_PATH，默认读生成流水线的产物。"""
    path = Path(os.environ.get(GRAPH_PATH_ENV, DEFAULT_GRAPH_PATH))
    build_server_from_path(path).run()


if __name__ == "__main__":  # pragma: no cover
    main()
