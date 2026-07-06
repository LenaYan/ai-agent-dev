# AI Agent 开发学习工作区

一个资深软件工程师系统学习 **AI Agent 开发** 的长期基地：文档、可运行 sample、实践项目，以及跨会话记忆。

## 目录结构

```
.
├── AGENTS.md                     # 基础配置：背景 + 协作规约（单一事实来源）
├── CLAUDE.md                     # Claude Code 入口，@导入 AGENTS.md（双工具支持）
├── .github/
│   ├── copilot-instructions.md   # Copilot 详细行为规则
│   └── instructions/             # 按路径生效的细分规则
├── docs/                         # 学习笔记、路线图、原理与对比
│   └── roadmap.md                # 学习路线图（从原理到上线）
├── samples/                      # 可运行的最小示例 / spike
├── practice/                     # 有结构的练习项目
├── notes/                        # 速记、临时素材
└── memory/                       # 跨会话记忆（AI 长期上下文）
    ├── README.md                 # 记忆运维规则
    ├── learning-log.md           # 学习日志
    ├── decisions.md              # 决策记录（ADR 风格）
    ├── glossary.md               # 术语表
    └── pitfalls.md               # 踩坑笔记
```

## 快速开始

1. 读 `docs/roadmap.md` 选一个当前阶段主题。
2. 在 `samples/` 或 `practice/` 建一个子目录动手。
3. 学完后按 `memory/README.md` 更新记忆。

## 约定

- 默认中文；密钥走 `.env`（已 gitignore），绝不提交。
- 每个 sample/practice 自带 `README.md`：这是什么、怎么跑、学到什么。
