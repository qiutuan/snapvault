"""M3 导入管线测试：去重合并 / 回收站恢复 / 命名落盘 / 损坏图片隔离 / 目录导入。"""
from __future__ import annotations

from pathlib import Path

import pytest

from snapvault.importer import Importer, validate_image


class TestImportFiles:
    def test_import_single(self, config, db, make_image):
        img = make_image("hello world 导入测试")
        report = Importer(db, config).import_files([img], source="dragdrop")
        assert len(report.imported) == 1
        asset = db.query_one("SELECT * FROM assets WHERE id=?", (report.imported[0],))
        assert asset["source"] == "dragdrop"
        assert asset["width"] > 0 and asset["height"] > 0
        assert Path(asset["path"]).exists()
        assert asset["path"].startswith(str(config.images_dir()))
        # 文件按规范命名
        assert Path(asset["path"]).name.endswith(".png")
        assert "_" in Path(asset["path"]).name

    def test_duplicate_merged(self, config, db, make_image):
        img = make_image("duplicate 去重测试")
        rep1 = Importer(db, config).import_files([img])
        rep2 = Importer(db, config).import_files([img])
        assert len(rep1.imported) == 1
        assert len(rep2.imported) == 0
        assert rep2.duplicates == [str(img)]
        assert db.scalar("SELECT COUNT(*) FROM assets") == 1

    def test_duplicate_keeps_tags_and_notes(self, config, db, make_image):
        img = make_image("带标签的截图")
        rep = Importer(db, config).import_files([img])
        aid = rep.imported[0]
        with db.tx() as conn:
            conn.execute("INSERT INTO tags(name) VALUES ('项目A')")
            tid = conn.execute("SELECT id FROM tags WHERE name='项目A'").fetchone()[0]
            conn.execute("INSERT INTO asset_tags(asset_id, tag_id) VALUES (?, ?)", (aid, tid))
            conn.execute("INSERT INTO notes(asset_id, content) VALUES (?, '重要备注')", (aid,))
        Importer(db, config).import_files([img])  # 重复导入
        tags = db.scalar("SELECT COUNT(*) FROM asset_tags WHERE asset_id=?", (aid,))
        notes = db.scalar("SELECT content FROM notes WHERE asset_id=?", (aid,))
        assert tags == 1 and notes == "重要备注"

    def test_trashed_restored(self, config, db, make_image):
        img = make_image("回收站恢复测试")
        rep = Importer(db, config).import_files([img])
        aid = rep.imported[0]
        db.execute("UPDATE assets SET deleted_at='2026-09-01T00:00:00Z', "
                   "deleted_reason='trash' WHERE id=?", (aid,))
        rep2 = Importer(db, config).import_files([img])
        assert rep2.restored == [aid]
        row = db.query_one("SELECT deleted_at FROM assets WHERE id=?", (aid,))
        assert row["deleted_at"] is None
        assert db.scalar("SELECT COUNT(*) FROM assets") == 1

    def test_corrupted_image_isolated(self, config, db, tmp_path):
        bad = tmp_path / "bad.png"
        bad.write_bytes(b"not a real image at all")
        report = Importer(db, config).import_files([bad])
        assert len(report.imported) == 0
        assert len(report.failed) == 1
        assert db.scalar("SELECT COUNT(*) FROM assets") == 0  # 不落任何半写入记录

    def test_unsupported_ext_skipped(self, config, db, tmp_path):
        f = tmp_path / "a.txt"
        f.write_text("hello")
        report = Importer(db, config).import_files([f])
        assert report.skipped_ext == [str(f)]
        assert db.scalar("SELECT COUNT(*) FROM assets") == 0

    def test_missing_file(self, config, db):
        report = Importer(db, config).import_files(["/no/such/file.png"])
        assert len(report.failed) == 1

    def test_import_enqueues_jobs(self, config, db, make_image):
        img = make_image("任务入队测试")
        rep = Importer(db, config).import_files([img])
        aid = rep.imported[0]
        types = {r["type"] for r in db.query(
            "SELECT type FROM jobs WHERE asset_id=?", (aid,))}
        assert "ocr" in types and "thumb" in types


class TestImportDirectory:
    def test_recursive_import(self, config, db, tmp_path):
        sub = tmp_path / "sub"
        sub.mkdir()
        from PIL import Image

        for i, name in enumerate(["a.png", "b.jpg", "c.webp"]):
            img = Image.new("RGB", (10 + i, 10), "white")
            img.save(sub / name)
        report = Importer(db, config).import_directory(tmp_path, recursive=True)
        assert len(report.imported) == 3
        assert db.scalar("SELECT COUNT(*) FROM assets") == 3

    def test_incremental_skip(self, config, db, tmp_path):
        from PIL import Image

        src = tmp_path / "src"
        src.mkdir()
        Image.new("RGB", (20, 20), "red").save(src / "a.png")
        Image.new("RGB", (21, 21), "blue").save(src / "b.png")
        rep1 = Importer(db, config).import_directory(src)
        rep2 = Importer(db, config).import_directory(src)  # 增量：全为重复
        assert len(rep1.imported) == 2
        assert len(rep2.imported) == 0 and len(rep2.duplicates) == 2

    def test_missing_dir(self, config, db):
        with pytest.raises(ValueError):
            Importer(db, config).import_directory("/no/such/dir")


class TestValidateImage:
    def test_valid(self, make_image):
        img = make_image("x")
        w, h, fmt = validate_image(img)
        assert w > 0 and h > 0 and fmt == "PNG"

    def test_invalid(self, tmp_path):
        p = tmp_path / "bad.png"
        p.write_bytes(b"garbage")
        with pytest.raises(ValueError):
            validate_image(p)
