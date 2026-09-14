# tool-call-loop — 零框架手写「LLM + 工具调用」最小循环

> 路线图**阶段一**的产出。对应 `memory/decisions.md` 的 **ADR-0007**：
> 新领域 agent 放 `samples/`，选域硬标准是 **ground truth 必须程序化可判定**。
> 领域选了 ADR-0007 的候选①：**沙箱内文件系统任务 agent**。

## 这是什么

一个 agent 在临时沙箱目录里，靠三个工具（`list_files` / `read_file` / `write_file`）
完成文件任务。任务是否完成由**程序断言沙箱终态**判定，不看模型说了什么。

零运行时依赖 —— HTTP 请求用 stdlib 直接发，**故意不用 SDK**，
因为阶段一要学的正是 tool calling 的线上格式本身，SDK 会把这层包起来。

## 心智模型：它和 DAG 流水线差在哪一行

`practice/cn-curriculum-graph` 是六层纯 DAG 流水线，下一步做什么写死在代码里：

```python
step3(step2(step1(x)))       # 控制流在开发期就确定，模型只是节点里的一个函数
```

这里的差别只有 `loop.py` 里的一行：

```python
if not reply.tool_calls:     # <-- 就是它
    return "done"
```

**退出时机、下一个调哪个工具、总共调几次，都由模型这一轮的输出决定，运行期才知道。**
控制流从代码里搬进了模型输出里 —— 这就是 workflow 与 agent 的分界线。

代价在同一行：控制流不可预测 → 必须有 `max_steps` 兜底，必须记轨迹才能复盘。
这两样都已经内建（`Trajectory` / `stop_reason`），为阶段二、三做准备。

## 怎么跑

### 1. 跑测试（不需要任何模型，现在就能跑）

```bash
cd samples/tool-call-loop
uv venv --python 3.12 .venv && uv pip install pytest
.venv/bin/python -m pytest -q      # 11 passed
```

测试用 `ScriptedLLM`（按剧本返回的假模型）验收**循环的性质**：
工具结果回灌、`tool_call_id` 对齐、模型停手就停、步数用尽被拦、
工具报错变成消息而不是异常、沙箱越界被挡、**判据本身能判错**。

> 最后一条不是凑数：2026-07-28 栽过「判据本身太脆」的跟头
> （见 ADR-0006 补记）。一个从不判 FAIL 的判据，全 PASS 毫无意义。

### 2. 接一个真实后端

本 sample 只认 **OpenAI 兼容的 `/chat/completions`**，Ollama、OpenRouter、
DeepSeek、vLLM、LM Studio 都说这套。在仓库根 `.env` 里设三个变量：

```bash
# 本地 Ollama（无需 key，需要挑一个支持 tool calling 的模型）
LLM_BASE_URL=http://localhost:11434/v1
LLM_MODEL=qwen3:8b
LLM_API_KEY=

# 或者 DeepSeek（OpenAI 兼容端点，注意不是 Anthropic 那个）
# LLM_BASE_URL=https://api.deepseek.com/v1
# LLM_MODEL=deepseek-chat
# LLM_API_KEY=sk-xxx
```

然后：

```bash
PYTHONPATH=src .venv/bin/python -m toolloop --all        # 跑全部任务
PYTHONPATH=src .venv/bin/python -m toolloop --task count_lines --keep
```

> ⚠️ **GitHub Models（`models.github.ai`）已退役**，2026-09-14 实测返回 **410 Gone**。
> GitHub Copilot 订阅**不提供**给第三方程序用的公开推理 API，这条路走不通。

## 任务集（判据都是终态断言）

| 任务 | 考什么 | 判据 |
|---|---|---|
| `create_readme` | 最少一次工具调用 | `README.md` 首行 == `# Demo` |
| `count_lines` | 必须先探索再汇总，步数不固定 | `total.txt` == `6`，且原始文件未被改动 |
| `toggle_debug` | 读-改-写，不许殃及其他内容 | `config.ini` 逐字节等于期望内容 |

`count_lines` 是这组里唯一真正需要"自己决定调几次"的：
模型得先 `list_files` 才知道有几个 `.txt`，且要自己排除 `.md`。

## 学到什么

1. **工具 schema 是模型唯一能看到的东西。** 描述写不好，模型就选不对工具——
   prompt 工程在 agent 里有一大半发生在工具描述上，不在 system prompt 里。
2. **工具失败必须作为普通的 `tool` 消息回给模型**，让它自己决定重试还是换路。
   抛异常中断循环，就退回成人写死的控制流了。（`tools.dispatch` 把所有异常吃成字符串）
3. **assistant 消息要原样塞回 `messages`。** `tool_calls` 的 `id` 要和后续 `tool` 消息
   一一对上，自己重建那个 dict 是踩坑重灾区 —— 所以 `Reply.raw` 保留了原始消息。
4. **沙箱要先 `resolve` 再比较。** 纯字符串前缀判断会被 `../..` 和符号链接绕过。
5. **假模型不是"没 key 时的替代品"，是让循环可测的前提。** 循环的正确性不该
   依赖一次真实采样 —— 真实模型的不确定性应该只用来测**模型**，不该用来测**代码**。

## 已知边界（不要假装做过了）

- **还没用真实模型跑过。** 本机无可用后端，全部验证走 `ScriptedLLM` + 本地假 HTTP 服务器。
  循环、协议解析、判据都验过了；**"真实模型会不会选对工具"这件事一次也没验过。**
- 只支持 OpenAI 兼容协议，没做 Anthropic 的 `tool_use` / `tool_result` 块格式。
- 没做并行工具调用的真正并行（模型一轮返回多个 `tool_calls` 时是顺序执行的）。
- 没有 ReAct 的显式思考提示、没有重规划、没有轨迹评估 —— 那是阶段二、三。
- 任务只有 3 个，`n=3`：**别拿它算通过率百分比**（ADR-0006 的教训）。
