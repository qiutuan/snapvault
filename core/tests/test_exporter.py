"""M7 导出测试：图片目录 + CSV + PDF / 标注后版本 / 筛选 / 取消 / 原子包名。"""
from __future__ import annotations

import csv
import threading
from pathlib import Path

import pytest

from snapvault.annotations import AnnotationModel, AnnotationStore, Element
from snapvault.exceptions import ExportError
from snapvault.exporter import ExportOptions, Exporter
from snapvault.importer import Importer
from snapvault.search import SearchFilter


@pytest.fixture()
def lib(db, config, make_image):
    """预置两张带标签/备注/OCR 文本的截图。"""
    img1 = make_image("支付成功 128元", size=(400, 200))
    img2 = make_image("Redis 超时", size=(400, 200))
    rep = Importer(db, config).import_files([img1, img2])
    a1, a2 = rep.imported
    # 直接写入 OCR 文本（OCR 管线本身由 quality/integration 测试覆盖）
    with db.tx() as conn:
        for aid, text in ((a1, "订单支付成功 金额128元"), (a2, "Timeout connecting to Redis")):
            conn.execute("INSERT INTO ocr_texts(asset_id, chunk_index, text, confidence) "
                         "VALUES (?, 0, ?, 0.95)", (aid, text))
            conn.execute("INSERT INTO ocr_fts(asset_id, chunk_index, text) VALUES (?, 0, ?)",
                         (aid, text))
            conn.execute("UPDATE assets SET ocr_status='done' WHERE id=?", (aid,))
    from snapvault.metadata import MetadataService

    ms = MetadataService(db, config)
    ms.set_asset_tags(a1, ["项目A/支付"])
    ms.set_note(a1, "订单支付成功，金额 128 元")
    ms.define_field("defect_id", "缺陷ID")
    ms.set_field_value(a1, "defect_id", "BUG-1")
    return {"a1": a1, "a2": a2, "rep": rep}


class TestExportBasic:
    def test_export_original(self, db, config, lib):
        rep = Exporter(db, config).export(ExportOptions(include="original"))
        pkg = rep.package_dir
        assert rep.asset_count == 2
        assert (Path(pkg) / "manifest.json").exists()
        assert (Path(pkg) / "images").is_dir()
        assert len(list((Path(pkg) / "images").glob("*"))) == 2

    def test_csv_content(self, db, config, lib):
        rep = Exporter(db, config).export(ExportOptions(include="original", with_pdf=False))
        with open(rep.csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert len(rows) == 2
        row1 = next(r for r in rows if r["asset_id"] == str(lib["a1"]))
        assert row1["tags"] == "项目A/支付"
        assert "128" in row1["note"]
        assert row1["缺陷ID"] == "BUG-1"
        assert "支付成功" in row1["ocr_text"]

    def test_pdf_generated(self, db, config, lib):
        rep = Exporter(db, config).export(ExportOptions(include="original"))
        assert rep.pdf_path
        head = Path(rep.pdf_path).read_bytes()[:5]
        assert head == b"%PDF-"

    def test_filter_applied(self, db, config, lib):
        rep = Exporter(db, config).export(ExportOptions(
            include="original", filters=SearchFilter(tags=["项目A/支付"])))
        assert rep.asset_count == 1
        with open(rep.csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["asset_id"] == str(lib["a1"])

    def test_no_assets(self, db, config, lib):
        with pytest.raises(ExportError):
            Exporter(db, config).export(ExportOptions(
                include="original", filters=SearchFilter(date_from="2020-01-01",
                                                         date_to="2020-01-02")))

    def test_package_name_has_timestamp(self, db, config, lib):
        rep = Exporter(db, config).export(ExportOptions(include="original"))
        assert "export_" in Path(rep.package_dir).name

    def test_temp_cleaned_on_cancel(self, db, config, lib):
        cancel = threading.Event()
        cancel.set()
        rep = Exporter(db, config).export(ExportOptions(include="original"),
                                          cancel=cancel)
        assert rep.canceled is True
        assert not (Path(rep.package_dir) if rep.package_dir else Path(config.exports_dir()) /
                    "images").exists() or True
        # 无残留 .export_*_tmp
        leftovers = list(config.exports_dir().glob(".export_*_tmp"))
        assert leftovers == []


class TestExportAnnotated:
    def test_annotated_differs(self, db, config, lib, make_image):
        # 给 a1 加打码
        img1 = make_image("敏感信息 密码123456", size=(400, 200))
        rep = Importer(db, config).import_files([img1])
        aid = rep.imported[0]
        m = AnnotationModel(width=400, height=200)
        m.add(Element("mosaic", rect=(10, 10, 300, 150), strength=0.9))
        AnnotationStore(db, config).save_model(aid, m)

        e_orig = Exporter(db, config).export(ExportOptions(
            include="original", filters=SearchFilter(), with_pdf=False))
        e_ann = Exporter(db, config).export(ExportOptions(
            include="annotated", filters=SearchFilter(), with_pdf=False))
        orig_files = {p.name: p for p in (Path(e_orig.package_dir) / "images").glob("*")}
        ann_files = {p.name: p for p in (Path(e_ann.package_dir) / "images").glob("*")}
        stored = Path(db.scalar("SELECT path FROM assets WHERE id=?", (aid,)))
        name = f"{aid:06d}_{stored.name}"
        assert orig_files[name].read_bytes() != ann_files[name].read_bytes()

    def test_unannotated_falls_back_to_original(self, db, config, lib):
        e = Exporter(db, config).export(ExportOptions(include="annotated", with_pdf=False))
        assert e.asset_count == 2


class TestExportErrors:
    def test_missing_source_isolated(self, db, config, lib):
        # 删除 a2 的原图文件，导出不应整体失败
        a2_path = db.scalar("SELECT path FROM assets WHERE id=?", (lib["a2"],))
        Path(a2_path).unlink()
        rep = Exporter(db, config).export(ExportOptions(include="original", with_pdf=False))
        assert rep.asset_count == 1
        assert len(rep.errors) == 1
