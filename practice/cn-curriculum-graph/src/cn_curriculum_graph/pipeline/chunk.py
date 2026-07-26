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
    # 统一换行：CRLF / 孤立 CR 都归一成 LF，避免 \r 混进落盘的 Chunk.text。
    text = text.replace("\r\n", "\n").replace("\r", "\n")

    stem = source_file.rsplit(".", 1)[0]
    chunks: list[Chunk] = []
    drops: list[DropRecord] = []

    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    for para_ordinal, para in enumerate(paragraphs, start=1):
        matched = _CODE.match(para)
        if matched is None:
            drops.append(
                DropRecord(
                    stage="chunk",
                    # 用原文段落序号，而非切出结果的长度反推 —— 语义直白，不怕以后
                    # 有分支既不进 chunks 也不进 drops 而悄悄错位。
                    ref=f"{stem}:{para_ordinal}",
                    reason="NO_STANDARD_CODE",
                    detail=para[:60],
                )
            )
            continue
        code, body = matched.group(1), matched.group(2).strip()
        # 注意：这里的 ordinal 是切出来的 chunk 的连续序号（跳过被丢弃的段落），
        # 与上面 DropRecord.ref 用的原文段落序号是两套不同的计数。
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
