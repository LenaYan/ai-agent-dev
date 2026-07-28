"""稳定 id 注册表：让节点身份活过一次重跑。

## 它解决什么

`make_topic_id` 原本是 `sha1(name|domain|grade_start)` —— 名字变一个字，id 就变。
而实测（`docs/pipeline-reproducibility.md`）：同一份课标原文、同一套代码、
`temperature=0` 跑三次，节点名两两 Jaccard 只有 29%~52%，**其中约七成不是内容
变了，是同义改写**：

    计数单位的感悟      →  计数单位
    了解十进制计数法    →  十进制计数法
    探索并掌握多位数的除法 → 多位数的除法
    比较万以内数的大小   →  万以内数的大小比较

于是 24 个评测标签一次重跑死了 12 个。**内容是稳的，抖的是措辞，而 id 挂在措辞上。**

## 为什么是"认领"而不是别的方案

四个候选在三次真实运行上量过（判据是"评测标签能否活过一次重跑"，
不循环）：

| 方案 | 标签存活 | 假合并 |
|---|---|---|
| `sha1(name)`（现状） | 58% | 0 |
| 归一化名称后哈希 | 58% | 0 |
| `chunk_id` + 序号 | **92%** | **5** |
| 相似度认领（本模块） | 83% | **0** |

`chunk_id + 序号` 的存活率最高，但它带来 5 个**假合并** —— 一个 chunk 这次出
6 个 draft、下次出 5 个，序号一错位，两个不同的知识点就共用了同一个 id。
**id 变动至少会被发现**（标签失效、评测拒跑），**假合并是静默的错误身份**，
下游拿着它做规划不会报错，只会算错。代价不对称，所以宁可少 9 个百分点。

归一化（剥掉"了解/探索并掌握"这类前缀、去掉"的"）几乎没用（58%→58%），
因为动荡不只是加减虚词，还有换角度重写（"字母只能表示一个固定的数" vs
"规律只能用具体的数字表示"）。

## 代价，写在这里而不是藏起来

**图不再是源的纯函数。** 性质从「同一份源 → 同一张图」变成
「同一份源 **+ 同一份注册表** → 同一张图」。注册表因此必须入库
（`data/topic-registry.json`）—— 按 ADR-0005，这个工作区是两台机器交替开发、
Git 是唯一同步通道，不入库的状态等于不存在。

**83% 不等于问题解决。** 24 个标签仍会死 4 个，评测的守卫闸门照样会红。
这个模块把"每次重跑重挂一半标签"降到"重挂几条"，没有消灭它。

**`CLAIM_THRESHOLD` 是旋钮不是常数。** 0.6 是从三次运行量出来的，不是从原理
推出来的：定高了认领不到（退回现状），定低了会把真正不同的知识点认成同一个。
一对一约束限制了损害范围，但不是零风险。换素材、换模型、换领域之后应当重估 ——
重估方法就是上表那套（跑三次，按标签存活率与假合并数一起看，**只看存活率会
选出错误的方案**）。
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Sequence

from cn_curriculum_graph.pipeline.models import DraftContent
from cn_curriculum_graph.serve.scoring import LiteralScorer

# 认领阈值：加权相关度达到这个数才算"同一个知识点"。
# 来源见模块文档 —— 实测值，不是推导值。
CLAIM_THRESHOLD = 0.6

# 名称与描述在认领得分里的权重。
#
# **为什么不是只看名称**：只看名称（且名称门槛 0.6）会漏掉名字改动大、但讲的是
# 同一件事的改名。实测里唯一一条被救回来的是「质数（素数）和合数」→「质数与合数」：
# 名称相关度只有 0.50（掉出门槛），描述相关度却是 0.91。加上描述这一项，
# 标签存活率从 79% 提到 83%，而且多认领的那一对经逐条核对是**对的**。
#
# **为什么不是只看描述**：描述比名称长得多，通用措辞（"在具体情境中…"）容易
# 把不相干的两条拉高。名称仍是主判据，描述只做辅助。
NAME_WEIGHT = 0.7
DESCRIPTION_WEIGHT = 0.3

_scorer = LiteralScorer()


def _mint(content: DraftContent) -> str:
    """首次登记时发一个新 id。沿用旧方案的种子，**这样第一次跑的 id 与
    引入注册表之前完全一致** —— 已有的评测标签不会因为这次改动而失效。"""
    seed = f"{content.name}|{content.domain}|{content.grade_start}"
    return "t_" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]


@dataclass
class RegistryEntry:
    id: str
    name: str
    """锚点：**首次登记时的名称，之后永不改动。**

    跟着最新名称走会让锚点一路漂移（"计数单位的感悟"→"计数单位"→"单位"），
    几轮之后它和最初那个知识点已经对不上了 —— 相似度是相对锚点算的，
    锚点一漂，认领就失准。
    """
    domain: str
    grade_start: int
    grade_end: int = 9
    description: str = ""
    """锚点的描述。与 name 一样首次登记后不再改动 —— 它是认领得分的第二个
    分量，专门用来救"名字改动大、讲的还是同一件事"那类改名。"""
    aliases: list[str] = field(default_factory=list)
    """历次认领时用过的其他名称。不参与匹配，只为人工复核留痕 ——
    别名列表越长，说明这个知识点的命名越不稳，值得看一眼是不是该拆或该合。"""


@dataclass
class TopicRegistry:
    entries: list[RegistryEntry]

    @classmethod
    def empty(cls) -> TopicRegistry:
        return cls(entries=[])

    @classmethod
    def load(cls, path: Path) -> TopicRegistry:
        """文件不存在就返回空表 —— 首次运行时它本来就不该存在，那是起点不是错误。"""
        if not path.exists():
            return cls.empty()
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(entries=[RegistryEntry(**e) for e in raw["entries"]])

    def save(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "_note": (
                "知识点 id 的稳定锚点。新一轮生成的节点按名称相似度认领这里的 id，"
                "认领不到才发新的 —— 让同义改写（'计数单位的感悟'→'计数单位'）不换身份。"
                "**这个文件必须入库**：不入库的话换台机器就退回'每次重跑 id 全变'。"
                "name 是首次登记的锚点，永不改动；aliases 是历次用过的其他名称，只为复核留痕。"
                "阈值的来历与代价见 src/cn_curriculum_graph/pipeline/topic_registry.py 模块文档。"
            ),
            "threshold": CLAIM_THRESHOLD,
            "entries": [
                {
                    "id": e.id,
                    "name": e.name,
                    "domain": e.domain,
                    "grade_start": e.grade_start,
                    "grade_end": e.grade_end,
                    "description": e.description,
                    "aliases": e.aliases,
                }
                for e in self.entries
            ],
        }
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
        )


def assign_topic_ids(
    contents: Sequence[DraftContent], registry: TopicRegistry
) -> list[str]:
    """给这一轮的知识点分配 id，就地更新 `registry`。**按输入顺序返回**。

    返回列表而不是 `{name: id}`：同名但学段不同是两个知识点，用名字做 key
    会让它们在返回值里撞掉 —— 而「同名不同学段」恰恰是这张图里真实存在的
    形状（分数的意义 G3-5 与 G5-6）。

    **贪心一对一**：按相似度从高到低配对，一个注册表条目只能被认领一次，
    一个新节点也只认领一次。没有这条约束，"计数单位"和"计数单位的意义"会
    双双认领同一个旧 id，两个不同的知识点就被合并了 —— 那正是本模块要避开
    的静默错误。

    **学段用区间重叠而不是起点全等**：`grade_start` 本身也在抖 —— 实测跨两轮的
    同一知识点里只有 78% 保持相同起点（"比较万以内数的大小" G2→G1）。用全等
    会把两成合法认领挡在门外（标签存活率 62% vs 区间重叠的 79%）。但 domain
    仍要求相同，且区间必须重叠：这两条挡住了"数位的含义 G2 → 比的含义 G6"
    这类靠通用后缀（"的含义"）凑到 0.6 的假配对。
    """
    assigned: dict[int, str] = {}
    claimed_entries: set[str] = set()

    candidates = []
    for i, c in enumerate(contents):
        for e in registry.entries:
            if e.domain != c.domain:
                continue
            if e.grade_end < c.grade_start or c.grade_end < e.grade_start:
                continue  # 学段区间不重叠 —— 不可能是同一个知识点
            score = (
                NAME_WEIGHT * _scorer.relevance(c.name, e.name)
                + DESCRIPTION_WEIGHT * _scorer.relevance(c.description, e.description)
            )
            if score >= CLAIM_THRESHOLD:
                candidates.append((score, i, c.name, e))

    # 排序键里带上名字，让同分时的先后确定 —— 否则同一份输入两次跑可能给出
    # 不同的配对，那就把这个模块要修的毛病又引回来了。
    for _score, i, name, entry in sorted(candidates, key=lambda t: (-t[0], t[2], t[1], t[3].id)):
        if i in assigned or entry.id in claimed_entries:
            continue
        assigned[i] = entry.id
        claimed_entries.add(entry.id)
        if name != entry.name and name not in entry.aliases:
            entry.aliases.append(name)

    for i, c in enumerate(contents):
        if i in assigned:
            continue
        new_id = _mint(c)
        assigned[i] = new_id
        registry.entries.append(
            RegistryEntry(
                id=new_id,
                name=c.name,
                domain=c.domain,
                grade_start=c.grade_start,
                grade_end=c.grade_end,
                description=c.description,
            )
        )

    return [assigned[i] for i in range(len(contents))]
