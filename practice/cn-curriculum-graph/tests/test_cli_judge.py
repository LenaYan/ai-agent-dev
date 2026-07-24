"""CLI 接 judge 的接线测试：--judge 选择是否激活 NAME_DESC_MISMATCH。

不触网、不需要 key —— 把 AnthropicJudge 换成 fake，只验证选择逻辑。
"""

import cn_curriculum_graph.cli as cli


def test_build_judge_none_returns_none():
    # 默认不接 judge：runner 会产出 CONSISTENCY_SKIPPED 警告而非静默通过
    assert cli.build_judge("none", model="whatever") is None


def test_build_judge_anthropic_constructs_with_model(monkeypatch):
    captured = {}

    class _FakeJudge:
        def __init__(self, model):
            captured["model"] = model

    monkeypatch.setattr(cli, "AnthropicJudge", _FakeJudge)
    judge = cli.build_judge("anthropic", model="claude-sonnet-5")

    assert isinstance(judge, _FakeJudge)
    assert captured["model"] == "claude-sonnet-5"
