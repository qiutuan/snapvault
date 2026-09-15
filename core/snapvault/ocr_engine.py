"""OCR 引擎：RapidOCR（ONNX Runtime）离线推理。

模型随包分发（core/snapvault/models/rapidocr/），运行时零网络请求。
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .exceptions import OCRUnavailableError

_MODEL_DIR = Path(__file__).resolve().parent / "models" / "rapidocr"


@dataclass
class OCRChunk:
    text: str
    confidence: float
    bbox: list[float]  # [x0, y0, x1, y1, x2, y2, x3, y3]（四点）
    lang: str = "ch+en"


@dataclass
class OCRResult:
    asset_id: int | None
    chunks: list[OCRChunk] = field(default_factory=list)
    elapsed_ms: float = 0.0

    def full_text(self) -> str:
        return "\n".join(c.text for c in self.chunks)

    def mean_confidence(self) -> float:
        if not self.chunks:
            return 0.0
        return sum(c.confidence for c in self.chunks) / len(self.chunks)


class OcrEngine:
    """线程安全的 RapidOCR 包装。引擎惰性加载，首次调用较慢。"""

    def __init__(self, model_dir: str | Path | None = None, num_threads: int = -1):
        self.model_dir = Path(model_dir) if model_dir else _MODEL_DIR
        self.num_threads = num_threads
        self._engine: Any | None = None
        self._lock = threading.Lock()

    # ------------------------------------------------------------------
    def _model_paths(self) -> dict[str, str]:
        expected = {
            "det_model_path": "ch_PP-OCRv4_det_infer.onnx",
            "rec_model_path": "ch_PP-OCRv4_rec_infer.onnx",
            "cls_model_path": "ch_ppocr_mobile_v2.0_cls_infer.onnx",
        }
        out = {}
        for kw, name in expected.items():
            p = self.model_dir / name
            if not p.exists():
                raise OCRUnavailableError(
                    f"OCR 模型缺失: {p}（应随 snapvault-core 安装包分发）"
                )
            out[kw] = str(p)
        return out

    def _get_engine(self) -> Any:
        if self._engine is None:
            with self._lock:
                if self._engine is None:
                    try:
                        from rapidocr_onnxruntime import RapidOCR

                        params = self._model_paths()
                        params["intra_op_num_threads"] = self.num_threads
                        self._engine = RapidOCR(**params)
                    except Exception as exc:  # noqa: BLE001
                        raise OCRUnavailableError(
                            f"OCR 引擎加载失败: {exc}"
                        ) from exc
        return self._engine

    # ------------------------------------------------------------------
    def is_available(self) -> bool:
        try:
            self._model_paths()
            return True
        except OCRUnavailableError:
            return False

    def recognize(self, image_path: str | Path) -> OCRResult:
        """识别单张图片，返回分块结果（按阅读顺序，行级）。"""
        engine = self._get_engine()
        import time

        t0 = time.perf_counter()
        result, _elapse = engine(str(image_path))
        elapsed = (time.perf_counter() - t0) * 1000.0
        res = OCRResult(asset_id=None, elapsed_ms=elapsed)
        if not result:
            return res
        for box, text, score in result:
            flat = [float(v) for point in box for v in point]
            res.chunks.append(
                OCRChunk(text=str(text), confidence=float(score), bbox=flat)
            )
        return res

    def recognize_bytes(self, data: bytes) -> OCRResult:
        """识别内存图片字节（用于剪贴板等无文件场景）。"""
        import io
        import time

        engine = self._get_engine()
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        import numpy as np

        arr = np.asarray(img.convert("RGB"))
        t0 = time.perf_counter()
        result, _ = engine(arr)
        elapsed = (time.perf_counter() - t0) * 1000.0
        res = OCRResult(asset_id=None, elapsed_ms=elapsed)
        if not result:
            return res
        for box, text, score in result:
            flat = [float(v) for point in box for v in point]
            res.chunks.append(
                OCRChunk(text=str(text), confidence=float(score), bbox=flat)
            )
        return res
