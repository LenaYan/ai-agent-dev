"""切分课标文本为带条目编号的片段 —— 纯规则，不需要模型。

输入契约：段落以空行分隔，每段首行以形如 `3.1.2` 的条目编号开头
（至少两级，点分数字；单级如"1."是列表序号，不算）。

切不出编号的段落直接丢弃并记 NO_STANDARD_CODE。这通常意味着切分规则
与该份素材的排版不匹配，是需要人看的信号，不该带病往下走。
"""

from __future__ import annotations

import re

from cn_curriculum_graph.pipeline.models import Chunk, DropRecord

# 至少两级的点分数字，如 3.1.2 / 4.2；后跟空白或全角空格
_CODE = re.compile(r"^\s*(\d+(?:\.\d+)+)[\s　]+(.*)", re.DOTALL)


def split_source(text: str, source_file: str) -> tuple[list[Chunk], list[DropRecord]]:
    stem = source_file.rsplit(".", 1)[0]
    chunks: list[Chunk] = []
    drops: list[DropRecord] = []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for para in paragraphs:
        matched = _CODE.match(para)
        if matched is None:
            drops.append(
                DropRecord(
                    stage="chunk",
                    ref=f"{stem}:{len(chunks) + len(drops) + 1}",
                    reason="NO_STANDARD_CODE",
                    detail=para[:60],
                )
            )
            continue
        code, body = matched.group(1), matched.group(2).strip()
        ordinal = len(chunks) + 1
        chunks.append(
            Chunk(
                id=f"{stem}#{ordinal:03d}",
                text=body,
                standard_code=code,
                source_file=source_file,
                ordinal=ordinal,
            )
        )

    return chunks, drops
