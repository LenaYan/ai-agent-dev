"""真 embedder 的接线测试 —— 不下载模型。

这层测试只回答一个问题：**没装可选依赖时，报错说不说人话**。
模型质量由 eval 量，模型加载由手动跑一次 eval 验证，都不在这里。
"""

import pytest

from cn_curriculum_graph.serve.embedding import DEFAULT_MODEL, build_embedder


def test_default_model_is_named_explicitly():
    """模型名是实验的一个变量，必须能一眼看到、一行换掉。"""
    assert DEFAULT_MODEL == "BAAI/bge-m3"


def test_missing_optional_dependency_says_how_to_fix_it(monkeypatch):
    """缺可选依赖时报一句 ModuleNotFoundError，用户得自己猜要装什么。
    这里钉住错误消息必须给出安装命令。"""
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    with pytest.raises(ImportError) as excinfo:
        build_embedder()

    assert "uv sync --extra embed" in str(excinfo.value)


def test_build_embedder_accepts_a_model_override():
    """换模型必须是一行 —— 这个实验的第二轮就是换模型再跑一遍。"""
    import inspect

    assert "model_name" in inspect.signature(build_embedder).parameters


def test_lazy_load_path_also_says_how_to_fix_missing_dependency(monkeypatch):
    """直接构造 BGEEmbedder、不经 build_embedder 的路径下，缺依赖时也得报人话。

    这锁定"延迟加载路径（首次 encode 时才加载模型）自己也会给出人话错误"这条性质。
    """
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name.startswith("sentence_transformers"):
            raise ModuleNotFoundError("No module named 'sentence_transformers'")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)

    from cn_curriculum_graph.serve.embedding import BGEEmbedder

    embedder = BGEEmbedder()  # 构造本身不报错（延迟加载）

    # 首次 encode 时才加载模型、才检查依赖
    with pytest.raises(ImportError) as excinfo:
        embedder.encode(["随便"])

    assert "uv sync --extra embed" in str(excinfo.value)
