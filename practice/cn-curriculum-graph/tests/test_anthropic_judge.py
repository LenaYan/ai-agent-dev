"""AnthropicJudge 的契约测试 —— 全程 mock 掉 Anthropic client，不触网、不需要 API key。

验证的是"接线"：judge 是否把 name/description 喂给了模型、是否把模型的结构化
回答翻译成了 Verdict。judge 判得准不准是另一回事，由 scripts/eval_judge.py
接真 LLM 对 ground truth 跑（那个才需要 key）。
"""

import json
from types import SimpleNamespace

from cn_curriculum_graph.judges.anthropic_judge import DEFAULT_MODEL, AnthropicJudge
from cn_curriculum_graph.validators.consistency import Verdict


class _Recorder(dict):
    """记录传给 messages.parse 的 kwargs，供断言。"""


def _fake_client(verdict: Verdict, recorder: _Recorder):
    def parse(**kwargs):
        recorder.update(kwargs)
        return SimpleNamespace(parsed_output=verdict)

    return SimpleNamespace(messages=SimpleNamespace(parse=parse))


def test_returns_verdict_from_model():
    recorder = _Recorder()
    client = _fake_client(Verdict(judgment="topic_mismatch", reason="名不符实：名说乘法，描述讲短除法"), recorder)

    judge = AnthropicJudge(client=client)
    verdict = judge(name="Arrays for multiplication", description="四位数除以一位数的短除法")

    assert verdict.judgment == "topic_mismatch"
    assert "短除法" in verdict.reason


def test_passes_name_and_description_and_model_to_request():
    recorder = _Recorder()
    client = _fake_client(Verdict(judgment="consistent"), recorder)

    judge = AnthropicJudge(client=client, model="claude-haiku-4-5")
    judge(name="三角形内角和", description="三角形三个内角的和是 180 度")

    # 请求里必须同时带上 name 与 description，否则模型无从判断
    payload = json.dumps(recorder, ensure_ascii=False, default=str)
    assert "三角形内角和" in payload
    assert "三角形三个内角的和是 180 度" in payload
    assert recorder["model"] == "claude-haiku-4-5"


def test_forces_verdict_schema_and_deterministic_sampling():
    recorder = _Recorder()
    client = _fake_client(Verdict(judgment="consistent"), recorder)

    AnthropicJudge(client=client)(name="平均数", description="一组数据的总和除以个数")

    # 结构化输出：必须约束成 Verdict，别让它自由发挥再解析
    assert recorder["output_format"] is Verdict
    # 分类任务要可复现
    assert recorder["temperature"] == 0


def test_default_model_is_haiku():
    # 默认走 Haiku：名实一致判断是简单有界任务，1590 次调用也便宜；
    # 评测证明不够再升级（构造参数可覆盖）。
    assert DEFAULT_MODEL == "claude-haiku-4-5"
