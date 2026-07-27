# data/source/ —— 课标原文语料（本地准备，不入库）

本目录的 `*.md` 是生成流水线的输入，内容是**课标原文**。

## 为什么目录是空的

`docs/feasibility-analysis.md` 的闸门 1 是"不对外分发课标文本"。本仓库公开，
所以语料一律 gitignore，只留这份说明。

> 历史遗留：此前 `data/source/example.md`（3 条真实课标条目）是被 git 跟踪的
> —— 那是这条闸门上的一个漏洞，已在补语料时一并修掉（`git rm --cached` +
> `.gitignore` 收口）。记在这里而不是悄悄删掉，因为"自己定的闸门自己漏了"
> 这件事本身比修复动作更值得留痕。

## 怎么准备语料

原文来源（2026-07-27 核实可下载）：教育部官网《义务教育数学课程标准（2022 年版）》

```
http://www.moe.gov.cn/srcsite/A26/s8001/202204/W020220420582346895190.pdf
```

注意这份 PDF 是**扫描件**（Adobe Image Conversion，无文字层），
`pdftotext` 提取结果为空 —— 需要 OCR，或逐页视觉转录。

## 格式契约

切分层（`pipeline/chunk.py`）是纯规则，对格式有硬要求：

- 段落之间用**空行**分隔
- 每段首行以**至少两级的点分编号**开头，后跟空白（半角或全角）
  - `2.1.3　在解决简单实际问题的过程中，……` ✅
  - `(3) 在解决简单实际问题的过程中，……` ❌（单级序号不算编号）
- 切不出编号的段落会被丢弃并记 `NO_STANDARD_CODE` —— 那是"切分规则与这份
  素材的排版不匹配"的信号，需要人看，不该带病往下走

编号自己编排即可，它只用于 `standard_codes` 溯源与 `LOW_STANDARDS_COVERAGE`
校验。本项目实际使用的编排是 `学段.主题.条目`：

| 段位 | 含义 |
|---|---|
| 第 1 位 | 学段（1=1\~2 年级，2=3\~4 年级，3=5\~6 年级） |
| 第 2 位 | 主题（1=数与运算，2=数量关系） |
| 第 3 位 | 该主题下的条目序号 |

## 干跑检查

放好语料后，先只跑切分确认格式对得上，再烧 LLM：

```bash
uv run python -c "
from pathlib import Path
from cn_curriculum_graph.pipeline.chunk import split_source
for p in sorted(Path('data/source').glob('*.md')):
    chunks, drops = split_source(p.read_text(encoding='utf-8'), source_file=p.name)
    print(f'{p.name}: 切出 {len(chunks)} 条，丢弃 {len(drops)} 条')
    for d in drops: print('  DROP', d.reason, d.detail[:40])
"
```
