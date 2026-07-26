"""CLI 接 judge 的接线测试：--judge 选择是否激活 NAME_DESC_MISMATCH。

不触网、不需要 key —— 把 AnthropicJudge 换成 fake，只验证选择逻辑。
"""

import pytest

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


def test_build_judge_deepseek_constructs_with_model(monkeypatch):
    captured = {}

    class _FakeJudge:
        def __init__(self, model):
            captured["model"] = model

    monkeypatch.setattr(cli, "DeepSeekJudge", _FakeJudge)
    judge = cli.build_judge("deepseek", model="deepseek-v4-pro")

    assert isinstance(judge, _FakeJudge)
    assert captured["model"] == "deepseek-v4-pro"


def test_each_judge_defaults_to_its_own_model():
    """--judge 换了模型默认值也要跟着换，否则会把 claude 的 id 发给 DeepSeek。"""
    assert cli.default_model_for("anthropic").startswith("claude-")
    assert cli.default_model_for("deepseek").startswith("deepseek-")


@pytest.mark.parametrize(
    ("kind", "env_var"),
    [("anthropic", "ANTHROPIC_API_KEY"), ("deepseek", "DEEPSEEK_API_KEY")],
)
def test_main_exits_cleanly_when_key_missing(kind, env_var, monkeypatch, tmp_path, capsys):
    """缺 key 要给一句人话 + 退出码 2，而不是甩一段 SDK 的原始 traceback。
    每个 judge 认自己的 key —— DeepSeek 绝不复用 ANTHROPIC_API_KEY。"""
    monkeypatch.delenv(env_var, raising=False)
    graph = tmp_path / "graph.json"
    graph.write_text("{}", encoding="utf-8")

    with pytest.raises(SystemExit) as excinfo:
        cli.main([str(graph), "--judge", kind])

    assert excinfo.value.code == 2
    assert env_var in capsys.readouterr().err
