"""M2 OCR 引擎测试：模型离线可用 / 真实推理中英文 / 结果结构。"""
from __future__ import annotations

import pytest

from snapvault.ocr_engine import OcrEngine

pytestmark = pytest.mark.ocr  # 需要离线 ONNX 模型（随包分发）


@pytest.fixture(scope="module")
def ocr():
    return OcrEngine()


class TestOcrEngine:
    def test_models_bundled(self, ocr):
        assert ocr.is_available()

    def test_recognize_chinese(self, ocr, make_image):
        img = make_image("订单支付成功 金额128元")
        result = ocr.recognize(img)
        text = result.full_text()
        assert "订单" in text and "支付" in text and "128" in text

    def test_recognize_english(self, ocr, make_image):
        img = make_image("Timeout connecting to Redis", size=(800, 200))
        result = ocr.recognize(img)
        text = result.full_text()
        assert "Timeout" in text and "Redis" in text

    def test_chunk_structure(self, ocr, make_image):
        img = make_image("第一行文字\n第二行文字", size=(800, 300))
        result = ocr.recognize(img)
        if result.chunks:
            c = result.chunks[0]
            assert c.text and 0.0 <= c.confidence <= 1.0
            assert len(c.bbox) == 8

    def test_elapsed_recorded(self, ocr, make_image):
        img = make_image("耗时统计")
        result = ocr.recognize(img)
        assert result.elapsed_ms >= 0

    def test_empty_image(self, ocr, make_image):
        from pathlib import Path

        from PIL import Image

        blank = Image.new("RGB", (100, 100), "white")
        tmp = Path(make_image("占位")).parent / "blank.png"
        blank.save(tmp)
        result = ocr.recognize(tmp)
        assert result.chunks == []
