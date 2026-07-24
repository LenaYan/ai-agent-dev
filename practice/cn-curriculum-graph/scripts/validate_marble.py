"""把校验层跑在 Marble 真实数据上，验证规则在规模下确实能抓到问题。

用法：
    uv run python scripts/validate_marble.py <os-taxonomy/data 目录>
"""

import json
import sys
from pathlib import Path

from cn_curriculum_graph.adapters.marble import from_marble
from cn_curriculum_graph.cli import format_report
from cn_curriculum_graph.runner import run_all


def main() -> int:
    data_dir = Path(sys.argv[1])
    topics = json.loads((data_dir / "topics.json").read_text(encoding="utf-8"))
    deps = json.loads((data_dir / "dependencies.json").read_text(encoding="utf-8"))

    graph = from_marble(topics, deps)
    print(f"载入 {len(graph.topics)} 节点 / {len(graph.dependencies)} 条边\n")
    print(format_report(run_all(graph)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
