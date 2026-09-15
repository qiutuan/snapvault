"""M3 命名模块测试：模板渲染 / 来源清洗 / 唯一性。"""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from snapvault.naming import normalize_source, render_filename, unique_filename

T = datetime(2026, 9, 15, 8, 5, 3, tzinfo=timezone.utc)


class TestRenderFilename:
    def test_default_template(self):
        name = render_filename("{date}_{time}_{source}.{ext}", when=T, source="capture", ext="png")
        assert name == "20260915_080503_capture.png"

    def test_datetime_token(self):
        name = render_filename("{datetime}_{source}.{ext}", when=T, source="clipboard", ext="jpg")
        assert name == "20260915_080503_clipboard.jpg"

    def test_source_sanitized(self):
        assert normalize_source("区域截图") == "区域截图"
        assert normalize_source("a/b c:d") == "a_b_c_d"
        assert normalize_source("  ") == "import"

    def test_seq_appended(self):
        name = render_filename("{date}_{time}_{source}.{ext}", when=T, source="import", ext="png", seq=3)
        assert name == "20260915_080503_import_3.png"

    def test_ext_normalized(self):
        assert render_filename("{date}_{time}_{source}.{ext}", when=T, source="x", ext="PNG") == "20260915_080503_x.png"

    def test_illegal_chars_removed(self):
        name = render_filename("{datetime}_{source}.{ext}", when=T, source="a<b>c", ext="png")
        assert "<" not in name and ">" not in name

    def test_no_trailing_underscore(self):
        name = render_filename("{date}_{time}_{source}_{seq}.{ext}", when=T, source="x", ext="png", seq=0)
        assert not name.endswith("_")


class TestUniqueFilename:
    def test_unique_with_seq(self, tmp_path):
        p = tmp_path / "20260915_080503_x.png"
        p.write_bytes(b"1")
        name = unique_filename(tmp_path, "{date}_{time}_{source}.{ext}", when=T, source="x", ext="png")
        assert name == "20260915_080503_x_1.png"

    def test_no_conflict_first(self, tmp_path):
        name = unique_filename(tmp_path, "{date}_{time}_{source}.{ext}", when=T, source="x", ext="png")
        assert name == "20260915_080503_x.png"
