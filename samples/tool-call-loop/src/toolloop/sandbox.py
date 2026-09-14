"""沙箱：把一切文件操作关进一个目录里。

这是阶段八「工具权限最小化」的最小版本，提前放在阶段一，
因为一旦循环真的开始自己决定调什么工具，没有围栏就不敢跑第二次。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class SandboxViolation(Exception):
    """模型试图碰沙箱外的路径。"""


@dataclass(frozen=True)
class Sandbox:
    root: Path

    @classmethod
    def create(cls, root: Path) -> "Sandbox":
        root = root.resolve()
        root.mkdir(parents=True, exist_ok=True)
        return cls(root=root)

    def resolve(self, relative: str) -> Path:
        """把模型给的相对路径解析成绝对路径，越界就抛。

        关键行：先 resolve 再比较。只做字符串前缀判断会被 `a/../../etc` 和
        符号链接绕过 —— resolve 会把两者都展平成真实路径。
        """
        candidate = (self.root / relative).resolve()
        if candidate != self.root and self.root not in candidate.parents:
            raise SandboxViolation(f"路径越界: {relative}")
        return candidate

    def snapshot(self) -> dict[str, str]:
        """沙箱的终态，用于程序化判定任务是否完成。"""
        return {
            str(p.relative_to(self.root)): p.read_text(encoding="utf-8")
            for p in sorted(self.root.rglob("*"))
            if p.is_file()
        }
