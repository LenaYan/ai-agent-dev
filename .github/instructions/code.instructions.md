---
applyTo: "samples/**,practice/**"
---

# 示例与练习代码规则

- Python 优先；用 venv/uv 隔离环境，依赖写进 `requirements.txt` 或 `pyproject.toml`。
- 每个子目录自带 `README.md`：这是什么、怎么跑、学到什么。
- 密钥/配置走环境变量或 `.env`（已 gitignore），**绝不硬编码或提交真实密钥**；提供 `.env.example` 占位。
- 代码以"能跑 + 易读"为先，学习场景不追求过度工程化。
- 涉及外部服务/模型时，在 README 注明所需密钥与预期成本/依赖。
