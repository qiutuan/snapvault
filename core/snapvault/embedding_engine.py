"""Embedding 引擎：fastembed（ONNX Runtime）本地推理 bge-small-zh-v1.5。

- 模型默认模型名 BAAI/bge-small-zh-v1.5（维度 512）
- 首次使用需一次性地通过 scripts/download_models.py 下载到数据目录 models/fastembed；
  此后完全离线推理，绝不调用任何云端 embedding API。
"""
from __future__ import annotations

import threading
from pathlib import Path

import numpy as np

from .exceptions import EmbeddingUnavailableError

DEFAULT_MODEL = "BAAI/bge-small-zh-v1.5"
EMBEDDING_DIM = 512


class EmbeddingEngine:
    """线程安全的 fastembed 包装。惰性加载。"""

    def __init__(self, cache_dir: str | Path, model_name: str = DEFAULT_MODEL):
        self.cache_dir = Path(cache_dir)
        self.model_name = model_name
        self._model = None
        self._lock = threading.Lock()
        self._load_failed: str | None = None

    # ------------------------------------------------------------------
    def _get_model(self):
        if self._model is None:
            with self._lock:
                if self._model is None:
                    try:
                        from fastembed import TextEmbedding

                        self._model = TextEmbedding(
                            model_name=self.model_name,
                            cache_dir=str(self.cache_dir),
                            threads=0,
                        )
                        self._load_failed = None
                    except Exception as exc:  # noqa: BLE001
                        self._load_failed = str(exc)
                        raise EmbeddingUnavailableError(
                            "Embedding 模型不可用。请先运行 scripts/download_models.py "
                            f"离线下载模型（{self.model_name}）。原始错误: {exc}"
                        ) from exc
        return self._model

    def is_available(self) -> bool:
        try:
            self._get_model()
            return True
        except EmbeddingUnavailableError:
            return False

    def embed(self, texts: list[str]) -> np.ndarray:
        """批量编码，返回 (n, 512) float32。空列表返回 (0,512)。"""
        if not texts:
            return np.zeros((0, EMBEDDING_DIM), dtype=np.float32)
        model = self._get_model()
        try:
            vectors = list(model.embed(texts))
            arr = np.asarray(vectors, dtype=np.float32)
            if arr.ndim != 2:
                raise EmbeddingUnavailableError("Embedding 输出维度异常")
            return arr
        except EmbeddingUnavailableError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise EmbeddingUnavailableError(f"Embedding 推理失败: {exc}") from exc

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]
