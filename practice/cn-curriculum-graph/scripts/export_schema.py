"""导出 JSON Schema，供非 Python 消费者（校验器、编辑器、其他语言）使用。"""

import json
from pathlib import Path

from cn_curriculum_graph.models import CurriculumGraph

OUT = Path(__file__).resolve().parent.parent / "schema" / "curriculum-graph.schema.json"


def main() -> int:
    schema = CurriculumGraph.model_json_schema()
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    schema["title"] = "中国课标知识依赖图"
    OUT.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"已导出 {OUT}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
