"""embedding 这一层的边界。

**本模块是整个 serve/ 里唯一允许 import 模型库的地方**（真实现在 Task 3
加进来）。`query.py` 与 `scoring.py` 只认下面这个协议，因此领域层的
全部单测都可以注入假 embedder，零依赖、零下载、毫秒级跑完（实测规模见
`docs/rag-vs-literal.md`）—— "全可测"这条性质靠这道边界保住。

手法与 `judges/` 完全一致：协议在领域侧，实现在外围，测试注入假的。
"""

from __future__ import annotations

from typing import Protocol


class Embedder(Protocol):
    """把文本批量编码成向量。

    单方法协议是刻意的：query/document 的不对称（有些模型要求查询侧加
    instruction 前缀）在实现内部消化，不外泄到协议 —— 否则领域层就得
    知道模型的脾气，这层边界就白划了。
    """

    def encode(self, texts: list[str]) -> list[list[float]]: ...


DEFAULT_MODEL: str = "BAAI/bge-m3"

_MISSING_DEP = (
    "向量检索需要可选依赖，未安装。装它：\n"
    "    uv sync --extra embed\n"
    "（实测 +744M 依赖、首次跑另下 4.3G 模型；默认的 uv sync 不装它，CI 也不装 —— "
    "领域层的单测靠假 embedder 跑，不需要模型。）"
)


def _require_sentence_transformers() -> None:
    """检查 sentence-transformers 是否可用，否则报出人话错误。

    由 build_embedder() 和 _ensure_model() 都调用，消除重复的依赖检查逻辑。
    """
    try:
        import sentence_transformers  # noqa: F401
    except ModuleNotFoundError as exc:
        raise ImportError(_MISSING_DEP) from exc


class BGEEmbedder:
    """`sentence-transformers` 的一层薄包装。

    **模型在首次 encode 时才加载，不在构造时**：eval 脚本会先构造再决定
    要不要跑，构造即加载会让 `--help` 都要等几十秒下模型。
    """

    def __init__(self, model_name: str = DEFAULT_MODEL, *, device: str | None = None) -> None:
        self.model_name = model_name
        self._device = device
        self._model = None

    def _ensure_model(self):
        if self._model is None:
            _require_sentence_transformers()
            from sentence_transformers import SentenceTransformer
            self._model = SentenceTransformer(self.model_name, device=self._device)
        return self._model

    def encode(self, texts: list[str]) -> list[list[float]]:
        """把文本批量编码成向量。

        依赖 sentence-transformers 返回 numpy 数组（显式传 convert_to_numpy=True）。
        """
        model = self._ensure_model()
        return [list(map(float, v)) for v in model.encode(texts, normalize_embeddings=True, convert_to_numpy=True)]


def build_embedder(model_name: str | None = None) -> Embedder:
    """工厂：eval 脚本唯一的入口。

    这里就地做一次依赖检查，好让"没装依赖"在脚本启动时报出人话，
    而不是等第一次 encode 时抛一句 ModuleNotFoundError。
    """
    _require_sentence_transformers()
    return BGEEmbedder(model_name or DEFAULT_MODEL)
