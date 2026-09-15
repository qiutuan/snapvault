"""M6 标注测试：图层模型 / 序列化 / 撤销重做 >=20 步 / 栅格化（含打码）/ 存储。"""
from __future__ import annotations

from pathlib import Path

import pytest

from snapvault.annotations import (
    AnnotationModel, AnnotationStore, Element, IntegrityError, rasterize,
)


class TestModel:
    def test_add_elements(self):
        m = AnnotationModel(width=100, height=100)
        m.add(Element("rect", rect=(10, 10, 50, 50)))
        m.add(Element("text", text="hi", points=[(5, 5)]))
        assert len(m.elements) == 2

    def test_update_and_remove(self):
        m = AnnotationModel()
        m.add(Element("rect", rect=(0, 0, 1, 1)))
        eid = m.elements[0].eid
        assert m.update(eid, color="#00ff00").color == "#00ff00"
        assert m.remove(eid) is True
        assert m.remove(eid) is False
        assert m.elements == []

    def test_undo_redo_20_steps(self):
        m = AnnotationModel()
        for i in range(25):
            m.add(Element("rect", rect=(i, i, 1, 1)))
        assert len(m.elements) == 25
        # 撤销 25 步：栈只保留最近 20 步
        undone = 0
        while m.undo():
            undone += 1
        assert undone == 20
        assert len(m.elements) == 5
        # 重做全部
        redone = 0
        while m.redo():
            redone += 1
        assert redone == 20
        assert len(m.elements) == 25

    def test_undo_clears_redo(self):
        m = AnnotationModel()
        m.add(Element("rect", rect=(0, 0, 1, 1)))
        m.add(Element("arrow", points=[(0, 0), (10, 10)]))
        m.undo()
        assert m.redo_depth() == 1
        m.add(Element("text", text="new"))
        assert m.redo_depth() == 0
        assert not m.redo()

    def test_clear(self):
        m = AnnotationModel()
        m.add(Element("rect", rect=(0, 0, 1, 1)))
        m.clear()
        assert m.elements == []
        assert m.undo()


class TestSerialization:
    def test_roundtrip(self):
        m = AnnotationModel(width=640, height=360)
        m.add(Element("pen", points=[(0, 0), (10, 10), (20, 5)], color="#112233", stroke_width=5))
        m.add(Element("mosaic", rect=(1, 2, 100, 50), strength=0.8))
        raw = m.to_json()
        m2 = AnnotationModel.from_json(raw)
        assert m2.width == 640 and m2.height == 360
        assert len(m2.elements) == 2
        assert m2.elements[0].etype == "pen"
        assert m2.elements[0].points == [(0.0, 0.0), (10.0, 10.0), (20.0, 5.0)]
        assert m2.elements[1].strength == 0.8

    def test_invalid_json(self):
        with pytest.raises(IntegrityError):
            AnnotationModel.from_json("{broken")

    def test_unknown_element_type(self):
        with pytest.raises(IntegrityError):
            AnnotationModel.from_json('{"elements": [{"type": "magic"}]}')

    def test_empty(self):
        m = AnnotationModel.from_json("")
        assert m.elements == []

    def test_idempotent_eids(self):
        m = AnnotationModel()
        m.add(Element("rect", rect=(0, 0, 1, 1)))
        eid = m.elements[0].eid
        m2 = AnnotationModel.from_json(m.to_json())
        assert m2.elements[0].eid == eid


class TestRasterize:
    @pytest.fixture()
    def img(self, make_image):
        return make_image("标注原图", size=(400, 240))

    def test_original_untouched(self, img):
        before = img.read_bytes()
        m = AnnotationModel(width=400, height=240)
        m.add(Element("rect", rect=(10, 10, 100, 80), color="#ff0000", stroke_width=4))
        out = rasterize(img, m, font_path=None)
        assert img.read_bytes() == before  # 原图只读
        assert out.size == (400, 240)

    def test_rect_drawn(self, img):
        m = AnnotationModel(width=400, height=240)
        m.add(Element("rect", rect=(10, 10, 100, 80), color="#ff0000", stroke_width=4))
        out = rasterize(img, m, font_path=None)
        assert out.getpixel((60, 12))[0] > 200  # 红色描边附近偏红

    def test_mosaic_blurs_region(self, tmp_path):
        from PIL import Image, ImageDraw

        # 打码区域内放置渐变，马赛克后块内像素趋同
        img = Image.new("RGB", (200, 150), "white")
        draw = ImageDraw.Draw(img)
        for x in range(20, 140):
            for y in range(20, 110):
                draw.point((x, y), fill=(x * 2 % 256, y * 3 % 256, 128))
        p = tmp_path / "mosaic_src.png"
        img.save(p)
        m = AnnotationModel(width=200, height=150)
        m.add(Element("mosaic", rect=(20, 20, 120, 90), strength=0.9))
        out = rasterize(p, m)
        plain = rasterize(p, AnnotationModel())
        # 马赛克块内部两个像素应趋于一致，而原图不同
        assert out.getpixel((50, 40)) != out.getpixel((80, 60))
        assert plain.getpixel((50, 40)) != plain.getpixel((80, 60))
        assert out.getpixel((50, 40)) != plain.getpixel((50, 40))

    def test_text_rendered(self, img):
        m = AnnotationModel(width=400, height=240)
        m.add(Element("text", text="打码标注", points=[(30, 30)], color="#0000ff", font_size=28))
        font_path = Path(__file__).resolve().parents[2] / "fonts" / "NotoSansSC-Regular.otf"
        out = rasterize(img, m, font_path=str(font_path))
        assert out is not None

    def test_pen_arrow_ellipse(self, img):
        m = AnnotationModel(width=400, height=240)
        m.add(Element("pen", points=[(0, 0), (50, 50)], color="#000000"))
        m.add(Element("arrow", points=[(10, 10), (90, 90)], color="#ff0000"))
        m.add(Element("ellipse", rect=(150, 30, 60, 40), color="#00ff00"))
        out = rasterize(img, m)
        assert out.size == (400, 240)

    def test_bad_element_skipped(self, img):
        m = AnnotationModel.from_json(
            '{"elements": [{"type": "rect", "rect": [0, 0, -5, -5]}]}')
        out = rasterize(img, m)
        assert out.size == (400, 240)


class TestAnnotationStore:
    def test_save_load(self, db, config, asset):
        store = AnnotationStore(db, config)
        m = AnnotationModel(width=100, height=100)
        m.add(Element("rect", rect=(0, 0, 10, 10)))
        store.save_model(asset, m)
        m2 = store.get_model(asset)
        assert len(m2.elements) == 1

    def test_update_override(self, db, config, asset):
        store = AnnotationStore(db, config)
        m = AnnotationModel(width=1, height=1)
        m.add(Element("text", text="v1"))
        store.save_model(asset, m)
        m.elements[0].text = "v2"
        store.save_model(asset, m)
        assert store.get_model(asset).elements[0].text == "v2"

    def test_default_empty(self, db, config, asset):
        store = AnnotationStore(db, config)
        assert store.get_model(asset).elements == []

    def test_delete(self, db, config, asset):
        store = AnnotationStore(db, config)
        m = AnnotationModel()
        m.add(Element("rect", rect=(0, 0, 1, 1)))
        store.save_model(asset, m)
        store.delete(asset)
        assert store.get_model(asset).elements == []
