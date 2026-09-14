"""命令行入口：`python -m toolloop --task count_lines`

后端靠三个环境变量决定（见 README）：
    LLM_BASE_URL / LLM_MODEL / LLM_API_KEY
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from pathlib import Path

from .llm import LLMError, OpenAICompatClient
from .loop import run_agent
from .sandbox import Sandbox
from .tasks import TASKS


def _load_dotenv() -> None:
    """极简 .env 读取，避免为一个 sample 引入 python-dotenv。"""
    for candidate in (Path.cwd() / ".env", Path(__file__).resolve().parents[3] / ".env"):
        if not candidate.is_file():
            continue
        for line in candidate.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            os.environ.setdefault(key.strip(), value.strip())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="toolloop", description="最小 tool calling 循环")
    parser.add_argument("--task", choices=sorted(TASKS), help="只跑一个任务")
    parser.add_argument("--all", action="store_true", help="跑全部任务")
    parser.add_argument("--keep", action="store_true", help="保留沙箱目录以便查看")
    parser.add_argument("--quiet", action="store_true", help="不打印每一步")
    args = parser.parse_args(argv)

    if not args.task and not args.all:
        parser.error("至少要给 --task 或 --all")

    _load_dotenv()
    base_url = os.environ.get("LLM_BASE_URL")
    model = os.environ.get("LLM_MODEL")
    if not base_url or not model:
        print(
            "缺少后端配置。请在 .env 里设置 LLM_BASE_URL 与 LLM_MODEL，"
            "例如本地 Ollama:\n"
            "  LLM_BASE_URL=http://localhost:11434/v1\n"
            "  LLM_MODEL=qwen3:8b\n"
            "详见 README「接一个真实后端」。",
            file=sys.stderr,
        )
        return 2

    client = OpenAICompatClient(
        model=model, base_url=base_url, api_key=os.environ.get("LLM_API_KEY", "")
    )

    names = sorted(TASKS) if args.all else [args.task]
    failures = 0
    for name in names:
        task = TASKS[name]
        workdir = Path(tempfile.mkdtemp(prefix=f"toolloop-{name}-"))
        sandbox = Sandbox.create(workdir)
        task.setup(sandbox)
        print(f"\n=== {name} ===\n任务: {task.prompt}\n沙箱: {sandbox.root}")
        try:
            traj = run_agent(
                task.prompt,
                sandbox,
                client,
                max_steps=task.max_steps,
                verbose=not args.quiet,
            )
        except LLMError as exc:
            print(f"调用模型失败: {exc}", file=sys.stderr)
            return 1
        ok, detail = task.check(sandbox)
        failures += 0 if ok else 1
        print(
            f"结果: {'PASS' if ok else 'FAIL'} ({detail}) | "
            f"停止原因={traj.stop_reason} | 工具调用={traj.steps_used} "
            f"轨迹={' -> '.join(traj.tool_names) or '(无)'}"
        )
        if args.keep:
            print(f"沙箱保留在 {sandbox.root}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
