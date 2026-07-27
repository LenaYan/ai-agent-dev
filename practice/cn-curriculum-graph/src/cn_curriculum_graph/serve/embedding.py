"""embedding 这一层的边界。

**本模块是整个 serve/ 里唯一允许 import 模型库的地方**（真实现在 Task 3
加进来）。`query.py` 与 `scoring.py` 只认下面这个协议，因此领域层的
55 条测试可以注入假 embedder，零依赖、零下载、毫秒级跑完 ——
"全可测"这条性质靠这道边界保住。

手法与 `judges/` 完全一致：协议在领域侧，实现在外围，测试注入假的。
"""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """把文本批量编码成向量。

    单方法协议是刻意的：query/document 的不对称（有些模型要求查询侧加
    instruction 前缀）在实现内部消化，不外泄到协议 —— 否则领域层就得
    知道模型的脾气，这层边界就白划了。
    """

    def encode(self, texts: list[str]) -> list[list[float]]: ...
