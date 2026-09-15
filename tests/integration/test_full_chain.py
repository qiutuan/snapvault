"""集成测试：导入 → OCR → 索引 → 检索 → 标注 → 导出 全链路。"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from snapvault.annotations import AnnotationModel, Element
from snapvault.exporter import ExportOptions
from snapvault.search import SearchFilter


class TestFullChain:
    def test_import_ocr_search_annotate_export(self, engine, make_shot):
        """核心闭环：导入 3 张 → OCR/向量 → 按文字搜回 → 打码 → 导出。"""
        texts = ["订单支付成功 金额128元", "Redis 集群超时重试", "JVM 内存溢出排查日志"]
        paths = [make_shot(t) for t in texts]
        rep = engine.import_files(paths, source="e2e")
        assert len(rep.imported) == 3

        n = engine.process_jobs()
        assert n >= 9  # 3×(ocr+embed+thumb)
        assert engine.job_stats().get("pending", 0) == 0

        # 关键词检索（FTS 精确命中）
        hits = engine.search("支付成功")
        assert hits and hits[0].asset_id == rep.imported[0]
        assert "支付" in hits[0].fragment

        # 语义检索（向量召回：输入近义描述应找回对应图）
        sem = engine.search("订单付款确认页面", k=3)
        assert sem and sem[0].asset_id == rep.imported[0]

        # 打码
        row = engine.db.query_one("SELECT width, height FROM assets WHERE id=?",
                                  (rep.imported[0],))
        model = AnnotationModel(width=row["width"], height=row["height"])
        model.add(Element("mosaic", rect=(10, 10, 200, 120), strength=0.9))
        engine.annotations.save_model(rep.imported[0], model)

        # 导出（标注版 + CSV + PDF）
        report = engine.export(ExportOptions(include="annotated", with_csv=True,
                                             with_pdf=True))
        pkg = Path(report.package_dir)
        assert (pkg / "images").exists()
        assert report.csv_path and Path(report.csv_path).exists()
        assert report.pdf_path and Path(report.pdf_path).exists()
        # 标注后图片与原图字节不同
        from PIL import Image

        ann = Image.open(pkg / "images" / "000001_0000000000_e2e.png" if
                         (pkg / "images" / "000001_0000000000_e2e.png").exists()
                         else sorted((pkg / "images").glob("000001_*"))[0])
        orig = Image.open(engine.db.query_one(
            "SELECT path FROM assets WHERE id=?", (rep.imported[0],))["path"])
        assert ann.tobytes() != orig.tobytes()

    def test_duplicate_merge_keeps_metadata(self, engine, make_shot):
        """同内容重复导入 → 去重合并，标签备注保留。"""
        p1 = make_shot("重复内容 ABC")
        rep1 = engine.import_files([p1])
        aid = rep1.imported[0]
        engine.metadata.set_asset_tags(aid, ["项目A/登录页"])
        engine.metadata.set_note(aid, "重复测试备注")
        engine.process_jobs()
        p2 = make_shot("重复内容 ABC")
        rep2 = engine.import_files([p2])
        assert len(rep2.duplicates) == 1
        assert engine.metadata.get_asset_tags(aid) == ["项目A/登录页"]
        assert engine.metadata.get_note(aid) == "重复测试备注"

    def test_recycle_bin_restore(self, engine, make_shot):
        p = make_shot("回收站测试 XYZ")
        aid = engine.import_files([p]).imported[0]
        engine.metadata.trash(aid)
        assert engine.db.scalar(
            "SELECT COUNT(*) FROM assets WHERE deleted_at IS NOT NULL") == 1
        engine.metadata.restore(aid)
        assert engine.db.scalar(
            "SELECT COUNT(*) FROM assets WHERE deleted_at IS NULL") == 1

    def test_custom_fields_exported(self, engine, make_shot):
        p = make_shot("字段导出测试")
        aid = engine.import_files([p]).imported[0]
        engine.process_jobs()
        engine.metadata.define_field("version", "版本号", ftype="text")
        engine.metadata.set_field_value(aid, "version", "v2.3.1")
        report = engine.export(ExportOptions(with_pdf=False, with_csv=True))
        import csv

        with open(report.csv_path, encoding="utf-8-sig") as f:
            rows = list(csv.DictReader(f))
        assert rows[0]["版本号"] == "v2.3.1"

    def test_export_filter_by_tag(self, engine, make_shot):
        p1 = make_shot("筛选甲 AAA111")
        p2 = make_shot("筛选乙 BBB222")
        a1 = engine.import_files([p1]).imported[0]
        a2 = engine.import_files([p2]).imported[0]
        engine.metadata.set_asset_tags(a1, ["缺陷/严重"])
        engine.metadata.set_asset_tags(a2, ["缺陷/一般"])
        f = SearchFilter(tags=["缺陷/严重"])
        report = engine.export(ExportOptions(filters=f, with_pdf=False, with_csv=False))
        images = list((Path(report.package_dir) / "images").glob("*"))
        assert len(images) == 1
        assert str(a1).encode() in images[0].name.encode()

    def test_similar_search(self, engine, make_shot):
        texts = ["首页按钮样式 A", "首页按钮样式 B", "支付流程截图", "设置页面截图"]
        ids = [engine.import_files([make_shot(t)]).imported[0] for t in texts]
        engine.process_jobs()
        hits = engine.similar(ids[0], k=3)
        assert hits and hits[0].asset_id in (ids[1],)  # 最相似应为同主题

    def test_rebuild_is_idempotent(self, engine, make_shot):
        p = make_shot("重建幂等测试 QQQ")
        engine.import_files([p])
        engine.process_jobs()
        rep1 = engine.rebuild_indexes(force=False)
        # 第二次重建应全部跳过（OCR 已 done、向量已存在、缩略图已存在）
        assert rep1.ocr_skipped == 1 and rep1.embed_done == 0
        rep2 = engine.rebuild_indexes(force=True)
        assert rep2.ocr_done == 1

    def test_privacy_scan(self, engine):
        report = engine.privacy_check()
        assert report.passed is True

    def test_backup_restore_roundtrip(self, engine, make_shot):
        p = make_shot("备份恢复测试 RRR")
        aid = engine.import_files([p]).imported[0]
        engine.metadata.set_note(aid, "备份备注")
        info = engine.backup.create_snapshot()
        engine.db.execute("DELETE FROM assets")
        n = engine.backup.restore_snapshot(info.path)
        assert n == 1
        assert engine.metadata.get_note(aid) == "备份备注"

    def test_cli_end_to_end(self, data_dir, make_shot, monkeypatch):
        """通过 CLI 子进程跑通 导入→搜索→导出。"""
        import subprocess
        import sys

        p = make_shot("CLI 端到端测试 CLI123")
        env = {"SNAPVAULT_DATA_DIR": str(data_dir),
               "PYTHONPATH": str(CORE := Path(__file__).resolve().parents[2] / "core"),
               "PATH": "/usr/bin:/bin"}
        def run(*args):
            return subprocess.run(
                [sys.executable, "-m", "snapvault.cli", *args],
                capture_output=True, text=True, env={**__import__("os").environ, **env},
            )
        r = run("import", str(p), "--process")
        assert r.returncode == 0, r.stderr
        r = run("search", "CLI123")
        assert r.returncode == 0 and "asset_id=1" in r.stdout
        r = run("export", "--include", "annotated", "--no-csv")
        assert r.returncode == 0 and "导出完成" in r.stdout
