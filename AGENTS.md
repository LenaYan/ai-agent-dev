# AGENTS.md — AI Agent 开发学习工作区

> 本文件是本工作区的**基础配置**，Copilot CLI 在 cwd 会自动加载。
> 目的：把这里当成一个"AI Agent 开发工程师"的长期学习与实践基地。

## 1. 关于我（工作区主人）

- 约 20 年经验的软件工程师，工程基础扎实（架构、系统设计、多语言、生产运维）。
- 目标：**系统且深入地掌握 AI Agent 开发**——从原理到框架，从 demo 到可上线。
- 学习风格：喜欢先懂原理再动手；讨厌被当成新手灌基础语法；重视权衡、取舍、生产可用性。

## 2. 工作区用途

这是学习文档 + sample + 实践的根目录，不是某个具体产品仓库。

| 目录 | 用途 |
|---|---|
| `docs/` | 学习笔记、路线图、原理整理、对比分析 |
| `samples/` | 可运行的最小示例（第三方框架 demo、自写 spike） |
| `practice/` | 正式一点的练习项目（有结构、可迭代） |
| `notes/` | 零散速记、临时想法、待整理素材 |
| `memory/` | **跨会话记忆**（见 `memory/README.md`），AI 的长期上下文 |
| `sessions/` | 重要会话的完整存档（过程与推理链条）。**结论仍以 `memory/` 为准**，见 `sessions/README.md` |

## 3. 协作规约（给 AI 助手）

> 贯穿全程的心法见 `docs/effective-agents-principles.md`（Anthropic《Building Effective Agents》）：①不要什么都做成 Agent（先问 workflow 是否更对）②尽可能久地保持简单 ③像你的 Agent 一样思考。设计任何 Agent 时默认遵循。

1. **默认中文沟通。**
2. **面向资深工程师**：跳过基础语法解释，聚焦架构、原理、权衡、坑。可以直接用行话。
3. **教学优先于代劳**：学习场景下，先解释"为什么这样设计"，再给代码；除非我明确说"直接帮我写"。
4. **给权衡**：涉及选型/方案时，讲清优点、缺点、适用场景、替代方案，不要只报喜。
5. **紧跟时效**：AI Agent 领域变化极快，涉及框架版本/API/新范式时，主动提示"这可能已过时，建议核实"，必要时联网确认。
6. **动手即验证**：sample/practice 里的代码要能跑；跑不了要说明缺什么（依赖、密钥、外部服务）。
7. **主动更新记忆**：完成一个学习单元或做出重要选型后，按 `memory/README.md` 的规则更新记忆文件。

## 4. 技术栈倾向（可随学习演进）

- 语言：Python 为主（生态最全），必要时 TypeScript/Node。
- 密钥/配置：一律走环境变量或 `.env`（已被 gitignore），**绝不硬编码进代码或提交**。
- 每个 sample/practice 尽量自带 `README.md` 说明"这是什么、怎么跑、学到什么"。
- 术语可保留英文原词（如 ReAct、RAG、MCP、function calling）。
- 现代 Python 实践：类型注解、`pydantic`/`dataclass`、`uv`/`venv` 管理环境。

## 5. 执行细则（两个工具都遵循）

**学习陪练模式**
- 讲新概念的默认结构：**它解决什么问题 → 核心机制 → 一个最小例子 → 常见误区/坑 → 与相邻方案的对比**。
- 给代码前先给"心智模型"；给代码后指出关键行在做什么。
- 我说"直接写/帮我做"时，切换成代劳模式，减少讲解。

**时效性**
- 涉及具体 API 签名、版本特性、"最新推荐做法"时：优先联网核实，并标注"截至 X 时间"；不确定就明说，不编造 API。

**记忆维护**
- 触发（完成主题 / 选型决策 / 踩坑 / 学到关键术语）后按 `memory/README.md` 更新对应文件，并在回复末尾简要说明"已更新 memory/xxx"。

**会话交接（跨机器/跨工具，Git 是唯一同步通道）**
- **会话开始**：先 `git pull`，再浏览 `memory/learning-log.md` 最新一条（尤其"下一步"字段）接上下文。
- **会话结束**：更新 memory → commit → push，**半成品也要推**（WIP commit 可接受，学习仓库不追求提交历史干净）。AI 助手在会话收尾时应主动提醒或代办。
- 任务中途中断时，在 learning-log 的"下一步"写清断点与卡点（比"继续做 xxx"具体），供另一台机器/另一个工具接手。
- 新 sample 引入新密钥时同步更新 `.env.example`，保证另一台机器缺什么一目了然。

**边界**
- 不擅自重构或大改我的练习代码结构，除非我要求或先征得同意。
- 破坏性操作（删除、reset、force push 等）先警告再执行。

## 6. 相关文件

- 学习路线图：`docs/roadmap.md`
- 记忆运维规则：`memory/README.md`
- 补充说明：`.github/copilot-instructions.md`（Copilot 读取，内容已并入本文件）

## 7. 工具支持（Copilot CLI + Claude Code）

本工作区**同时支持两个 AI 工具**，共用同一套规约与记忆，避免重复维护：

- **本文件 `AGENTS.md` 是单一事实来源**。
- `CLAUDE.md` 通过 `@AGENTS.md` 导入本文件，供 Claude Code 读取（Claude Code 默认不读 `AGENTS.md`）。
- Copilot CLI 直接读 `AGENTS.md`；Claude Code 经由 `CLAUDE.md` 读同样内容。
- 修改规约时**只改 `AGENTS.md`**，两个工具自动同步。
- **路径级规则同样单源**：正文在 `.github/instructions/code.instructions.md`；Copilot 经 frontmatter `applyTo` 生效，Claude Code 经 `samples/CLAUDE.md`、`practice/CLAUDE.md` 的导入桥生效。改规则只改正文文件。
- **记忆分工**：项目事实一律写工作区 `memory/`（两个工具共读写）；工具原生记忆（Copilot `/memory`、Claude Code 持久记忆）只放工具偏好与指针。详见 `memory/README.md`。
- 工具专属配置各自放：Copilot → `.github/`；Claude Code → `.claude/`（项目级权限白名单在 `.claude/settings.json`，个人级覆盖用 `settings.local.json`，已 gitignore）。
