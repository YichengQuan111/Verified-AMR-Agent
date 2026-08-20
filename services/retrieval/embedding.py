"""Qwen3-Embedding-0.6B 的独立、可注入 Embedder 封装。"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Sequence

import numpy as np


class Embedder:
    """分别提供文档与查询编码，并从实际模型动态读取维度。

    Qwen3 SentenceTransformer 配置为 query/document 定义了不同 prompt；混用会降低
    召回，因此两个公共方法明确分开。测试可以注入轻量 model，但生产路径始终
    使用本地目录且不触发网络下载。
    """

    def __init__(
        self,
        model_path: str | Path,
        *,
        device: str = "cpu",
        batch_size: int = 8,
        model: Any | None = None,
    ) -> None:
        if batch_size < 1:
            raise ValueError("batch_size 必须大于 0")
        self.model_path = Path(model_path)
        self.device = device
        self.batch_size = batch_size
        if model is None:
            if not self.model_path.is_dir():
                raise FileNotFoundError(f"Embedding 模型目录不存在: {self.model_path}")
            # 延迟导入避免仅使用 Loader/BM25 的进程加载 torch/transformers。
            from sentence_transformers import SentenceTransformer

            model = SentenceTransformer(
                str(self.model_path),
                device=device,
                local_files_only=True,
            )
        self._model = model
        self.dimension = self._read_dimension(model)

    @staticmethod
    def _read_dimension(model: Any) -> int:
        """兼容当前和旧版 SentenceTransformer 的维度读取方法。"""

        getter = getattr(model, "get_embedding_dimension", None)
        if getter is None:
            getter = getattr(model, "get_sentence_embedding_dimension", None)
        if getter is None:
            raise TypeError("Embedding 模型没有维度读取接口")
        dimension = getter()
        if not isinstance(dimension, int) or dimension <= 0:
            raise ValueError(f"Embedding dimension 无效: {dimension!r}")
        return dimension

    def embed_documents(self, texts: Sequence[str]) -> np.ndarray:
        """批量编码文档，返回形状 ``(n, dynamic_dimension)`` 的 float32。"""

        if not texts:
            return np.empty((0, self.dimension), dtype=np.float32)
        if any(not text.strip() for text in texts):
            raise ValueError("待编码文档不能包含空文本")
        return self._encode(list(texts), prompt_name="document")

    def embed_query(self, query: str) -> np.ndarray:
        """使用 Qwen3 query prompt 编码单条检索问题。"""

        if not query.strip():
            raise ValueError("检索 query 不能为空")
        matrix = self._encode([query], prompt_name="query")
        return matrix[0]

    def _encode(self, texts: list[str], *, prompt_name: str) -> np.ndarray:
        """统一执行归一化与形状/数值防御，阻止错误向量写入 Qdrant。"""

        encoded = self._model.encode(
            texts,
            prompt_name=prompt_name,
            batch_size=self.batch_size,
            normalize_embeddings=True,
            convert_to_numpy=True,
            show_progress_bar=False,
        )
        matrix = np.asarray(encoded, dtype=np.float32)
        if matrix.ndim == 1:
            matrix = matrix.reshape(1, -1)
        expected_shape = (len(texts), self.dimension)
        if matrix.shape != expected_shape:
            raise ValueError(
                f"Embedding shape 不匹配: {matrix.shape} != {expected_shape}"
            )
        if not np.isfinite(matrix).all():
            raise ValueError("Embedding 包含 NaN 或无穷值")
        return matrix


__all__ = ["Embedder"]
