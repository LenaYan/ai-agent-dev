# 字面 vs 向量对比：收尾 review 的延后项

> 来源：2026-07-27 那轮受控对比的逐任务 review + 最终整支 review。
> 已修的部分见 git 历史（`081a1dd..a70b406`）；这里只列**分诊为"可延后"或"不必修"、因而没做**的。
> 实验结论本身见 `rag-vs-literal.md`，设计与判据见 `superpowers/specs/2026-07-27-rag-vs-literal-design.md`。

写下来是因为这些条目分散在十几份 review 报告里，而那些报告是临时文件。
**它们都不影响那轮的结论** —— 结论的可信度已由代码结构保证（同工作点比较、
字面基线现算且扫描区间解耦、够不到工作点就非零退出），这三道防线都有测试。

## 必须在下一轮开始前处理（不能在轮中改）

**向量缓存 key 用的是未 strip 的原文**（`serve/scoring.py`）。`"甲"` 与 `" 甲 "`
会各占一条缓存、各编码一次。对上一轮结论零影响（两条路线看到的是同一份文本）。

**为什么不能在轮中改**：只 strip key 而 encode 原文，会让同一个 key 对应两个不同
向量，比现状更糟；要改就得 key 和送去 encode 的文本一起 strip，而那会让分数发生
微小变化 —— 等于动了实验条件。所以它属于"下一轮开始前"，不属于"随手修"。

## 会静默出错的（症状不明显，值得优先）

**`_corpus_texts()` 与检索函数各自记着"哪些字段可检索"**（`serve/query.py`）。
`_corpus_texts` 列 name/description/statement/probe/correction_hint，而
`search_topics` 与 `match_misconceptions` 各自独立地重复这份知识。加一个可检索
字段却忘了同步 `_corpus_texts`，**向量路径不会报错，只会悄悄退化成按需 encode**
—— 建索引耗时与 `encode_calls` 两个代价数字会错一个数量级，而测试全绿。
一条"`_corpus_texts()` 的元素集合 ⊇ 检索函数实际打分的文本集合"的测试就够。

**字面单跑 `--threshold-sweep` 不打印实际扫描区间**（`scripts/eval_diagnosis.py`）。
`--scorer literal --threshold-sweep --sweep-from 0.3` 会打印"同工作点：阈值 0.3 →
recall@3 = 68%"，不显示这是在收窄区间上算的。`LITERAL_BASELINE_SWEEP` 那道防线
只保护了 vector 分支里那行对比（那里已打印区间），**字面单跑这条路径上同一种
自欺仍然可达**。修法一行：表头打印扫描区间与步长。

**扫描模式不过 `RECALL_AT_3_THRESHOLD` 闸门**（`scripts/eval_diagnosis.py`）。
逐条模式走 `verdict()` 的 75% 闸门，扫描模式只要拿得到工作点就 `return 0`。
多半是刻意的（扫描是探索性的），但两种模式退出码语义不同这件事没写在任何地方。
至少加一句注释说明这是有意的。

## 文档债

**死符号**：文档里还在引用 `MIN_RELEVANCE`（Task 1 已改名为
`LiteralScorer.min_relevance`）；`scripts/eval_diagnosis.py` 里还印着 `MIN_COVERAGE`
—— 这个名字**从未存在过**，是更早一轮的遗留，而且它出现在**跑失败时给用户看的
提示**里（"下一步不是调 MIN_COVERAGE"）。后者是本轮之前的债，前者是本轮制造的。

**`memory/decisions.md` 里对"字面基线 84%"的更正没用删除线**，与
`learning-log.md` 的处理规格不一致（那边用了 `~~删除线~~`），而 decisions.md
恰恰是更容易被后来者单独引用的一份。另外措辞可以更准：**84% 在阈值 0.2 上是
正确的读数，错的是拿它当基线** —— `rag-vs-literal.md` 说得准确，decisions.md 可以对齐。

**模型体积 4.3G 缺对应的命令输出**。值已被独立核实为真，且已注明它在
`~/.cache/huggingface/hub`（仓库外），读者自己 `du -sh` 一条命令的事。
但在一篇以"每个数字可追溯"为卖点的笔记里，这是唯一一处断链。

## 代码整洁（不影响正确性）

- **`Scorer.min_relevance` 两个实现写法不统一**：`LiteralScorer` 是类属性、
  `VectorScorer` 是实例属性。mypy/pyright 都不会告警（前者没标 `ClassVar`）。
  真正的收益在别处：改成 `__init__(self, *, min_relevance: float = 0.2)` 后，
  能**整段删掉** `eval_diagnosis.py` 里那段"类属性被实例属性遮蔽"的免责注释
  —— 用五行代码换掉四行需要读者理解的坑。
- **`VectorScorer._encode_into_cache` 的"返回条数 ≠ 请求条数"防御分支无测试**。
  它抛 `ValueError`、不会静默，所以不补也不掩盖任何真实缺陷。
- **`BGEEmbedder._ensure_model()` 缺返回类型注解与 docstring**。补返回类型需要在
  `TYPE_CHECKING` 守卫下 import `SentenceTransformer`，为一个私有方法引入这层仪式
  未必划算。
- **跨任务重复测试**：`tests/serve/test_query.py` 里对 `normalize_math` 的断言与
  `tests/serve/test_scoring.py` 重叠。这是 Task 1 刻意不动 `test_query.py`
  （为了证明重构等价）加上后续新增测试的必然产物，五个任务做完没人回收。
  前者按分层该整体挪进 `test_scoring.py`；测端到端行为的那两条留在原处是对的。

## 已剔除

**"净退步被单独引用会丢掉统计限定"** —— 前提事实不成立。全文 grep 确认
README 里根本没有"净退步"三个字，它只出现在 `rag-vs-literal.md` 中，而那一段
的上下文标题就是"10 个百分点在 n=19 上不是显著提升"。
