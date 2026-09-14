# samples/ — 可运行最小示例

存放最小可跑的 demo 与 spike，一个主题一个子目录。

每个子目录自带 `README.md`：**这是什么 · 怎么跑 · 学到什么**。
密钥用 `.env`（已被根目录 gitignore），不要提交真实密钥。

示例约定：
```
samples/
  01-native-tool-calling/
    README.md
    main.py
    requirements.txt (或 pyproject.toml)
```

## 目录

| 子目录 | 主题 | 路线图阶段 |
|---|---|---|
| `tool-call-loop/` | 零框架手写「LLM + 工具调用」最小循环（沙箱文件系统任务 agent，带程序化终态断言） | 阶段一 |
