"""DeepSeekJudge 的契约测试 —— 全程 mock client，不触网、不需要 key。

与 AnthropicJudge 的关键差异在结构化输出的拿法：DeepSeek 的 Anthropic 兼容端点
**不遵守** `output_format`（实测会照收参数但返回自由文本），所以改用
"强制调用一个 input_schema=Verdict 的工具"这条跨 provider 通用的路子。
这些测试就是把那个决定钉住。
"""

import pytest
from types import SimpleNamespace

from cn_curriculum_graph.judges.deepseek_judge import (
    DEEPSEEK_BASE_URL,
    DEFAULT_MODEL,
    VERDICT_TOOL_NAME,
    DeepSeekJudge,
)
from cn_curriculum_graph.validators.consistency import Verdict


class _Recorder(dict):
    """记录传给 messages.create 的 kwargs，供断言。"""


def _fake_client(recorder: _Recorder, *, tool_input=None, content=None):
    def create(**kwargs):
        recorder.update(kwargs)
        blocks = content
        if blocks is None:
            blocks = [
                SimpleNamespace(
                    type="tool_use",
                    name=VERDICT_TOOL_NAME,
                    input=tool_input if tool_input is not None else {"consistent": True, "reason": ""},
                )
            ]
        return SimpleNamespace(content=blocks)

    return SimpleNamespace(messages=SimpleNamespace(create=create))


def test_returns_verdict_from_tool_call():
    recorder = _Recorder()
    client = _fake_client(
        recorder,
        tool_input={"consistent": False, "reason": "名说乘法阵列，描述在讲短除法"},
    )

    verdict = DeepSeekJudge(client=client)(
        name="Arrays for multiplication", description="四位数除以一位数的短除法"
    )

    assert isinstance(verdict, Verdict)
    assert verdict.consistent is False
    assert "短除法" in verdict.reason


def test_passes_name_and_description_and_model_to_request():
    recorder = _Recorder()
    client = _fake_client(recorder)

    DeepSeekJudge(client=client, model="deepseek-v4-pro")(
        name="三角形内角和", description="三角形三个内角的和是 180 度"
    )

    payload = str(recorder["messages"])
    assert "三角形内角和" in payload
    assert "三角形三个内角的和是 180 度" in payload
    assert recorder["model"] == "deepseek-v4-pro"


def test_forces_verdict_tool_with_thinking_disabled_and_deterministic_sampling():
    recorder = _Recorder()
    client = _fake_client(recorder)

    DeepSeekJudge(client=client)(name="平均数", description="一组数据的总和除以个数")

    # 结构化输出：强制走工具，别让它自由发挥再解析
    assert recorder["tool_choice"] == {"type": "tool", "name": VERDICT_TOOL_NAME}
    # DeepSeek 实测：thinking 模式下不接受强制 tool_choice（400），且分类任务不需要它
    assert recorder["thinking"] == {"type": "disabled"}
    # 分类任务要可复现
    assert recorder["temperature"] == 0


def test_tool_schema_comes_from_verdict():
    recorder = _Recorder()
    client = _fake_client(recorder)

    DeepSeekJudge(client=client)(name="平均数", description="一组数据的总和除以个数")

    (tool,) = recorder["tools"]
    assert tool["name"] == VERDICT_TOOL_NAME
    # schema 由 Verdict 自己导出，避免手写 schema 与模型漂移
    assert tool["input_schema"] == Verdict.model_json_schema()
    assert tool["input_schema"]["additionalProperties"] is False


def test_raises_clear_error_when_model_skips_the_tool():
    recorder = _Recorder()
    # 模型没调工具，只回了段文本
    client = _fake_client(recorder, content=[SimpleNamespace(type="text", text="我觉得不一致")])

    with pytest.raises(ValueError, match=VERDICT_TOOL_NAME):
        DeepSeekJudge(client=client)(name="平均数", description="一组数据的总和除以个数")


def test_default_model_is_v4_flash():
    # 便宜档；评测证明不够再升 deepseek-v4-pro（构造参数可覆盖）
    assert DEFAULT_MODEL == "deepseek-v4-flash"


def test_default_client_targets_deepseek_endpoint_with_its_own_key(monkeypatch):
    """绝不复用 ANTHROPIC_API_KEY，也绝不靠 ANTHROPIC_BASE_URL 环境变量 ——
    后者会把同机运行的 Claude Code 一起劫持到 DeepSeek 上去。"""
    import anthropic

    captured = {}

    class _FakeAnthropic:
        def __init__(self, **kwargs):
            captured.update(kwargs)

    monkeypatch.setattr(anthropic, "Anthropic", _FakeAnthropic)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "sk-test-key")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-must-not-be-used")

    DeepSeekJudge()

    assert captured["base_url"] == DEEPSEEK_BASE_URL
    assert captured["api_key"] == "sk-test-key"
