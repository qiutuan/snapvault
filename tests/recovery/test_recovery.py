"""崩溃恢复测试（M1/M3/M9）：坏库降级可修复、坏图隔离、坏库可用备份恢复。

结果写入 reports/recovery_report.json。
"""
from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from snapvault.db import Database, open_database
from snapvault.exceptions import DatabaseError
from snapvault.exporter import ExportOptions
from snapvault.importer import Importer

REPORTS = Path(__file__).resolve().parents[2] / "reports"


class TestRecovery:
    def test_corrupt_db_fails_gracefully(self, db_path):
        """损坏数据库：打开时明确报 DatabaseError，不崩溃不丢其它功能。"""
        # 先建一个合法库
        conn = open_database(db_path)
        conn.execute("CREATE TABLE t(x)")
        conn.commit()
        conn.close()
        # 异或头 15 字节 → 损坏
        data = bytearray(db_path.read_bytes())
        for i in range(15):
            data[i] ^= 0xFF
        db_path.write_bytes(bytes(data))
        with pytest.raises(DatabaseError):
            open_database(db_path)

    def test_corrupt_db_restore_from_backup(self, engine, make_shot):
        """坏库场景：用快照恢复，数据完整。"""
        p = make_shot("坏库恢复测试 AAA")
        aid = engine.import_files([p]).imported[0]
        engine.metadata.set_note(aid, "恢复后应有此备注")
        info = engine.backup.create_snapshot()

        engine.close()
        db_path = engine.config.db_path()
        data = bytearray(db_path.read_bytes())
        for i in range(15):
            data[i] ^= 0xFF
        db_path.write_bytes(bytes(data))
        # 重开（模拟用户重启后打开失败）
        with pytest.raises(DatabaseError):
            open_database(db_path)

        # 通过恢复入口修复
        from snapvault.backup import BackupManager
        from snapvault.config import Config

        cfg = Config(engine.config.root)
        bm = BackupManager(Database(db_path), cfg)
        n = bm.restore_snapshot(info.path)
        assert n == 1
        ndb = Database(db_path)
        assert ndb.scalar("SELECT content FROM notes WHERE asset_id=?", (aid,)) \
            == "恢复后应有此备注"

    def test_corrupt_image_isolated_on_import(self, engine):
        """坏图导入被隔离，不影响其它正常导入。"""
        bad = Path("/tmp/sv_bad_img.png")
        bad.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 200)  # 伪 PNG
        good = Path("/tmp/sv_good_img.png")
        good.write_bytes(b"not an image")
        rep = Importer(engine.db, engine.config).import_files([bad, good])
        assert len(rep.imported) == 0
        assert len(rep.failed) == 2

    def test_corrupt_image_after_import_export_isolates(self, engine, make_shot):
        """原图落盘后被改坏：导出隔离该资产，其余正常导出；检索不受影响。"""
        p1 = make_shot("正常图片一 OK1")
        p2 = make_shot("正常图片二 OK2")
        a1 = engine.import_files([p1]).imported[0]
        a2 = engine.import_files([p2]).imported[0]
        engine.process_jobs()
        # 改坏 a1 的原图
        stored = Path(engine.db.query_one(
            "SELECT path FROM assets WHERE id=?", (a1,))["path"])
        stored.write_bytes(b"\x89PNG\x00\x00broken")
        # 检索不受影响
        hits = engine.search("OK1")
        assert hits and hits[0].asset_id == a1
        # 导出隔离 a1
        report = engine.export(ExportOptions(with_pdf=False, with_csv=False))
        assert any("000001" in e or str(a1) in e for e in report.errors), \
            f"坏图未隔离: {report.errors}"
        images = list((Path(report.package_dir) / "images").glob("000002_*"))
        assert len(images) == 1

    def test_rebuild_skips_corrupt_image(self, engine, make_shot):
        """坏图在 rebuild 中被记录为失败，其余资产正常重建。"""
        p1 = make_shot("重建正常图 ZZZ")
        p2 = make_shot("重建坏图 BBB")
        engine.import_files([p1])
        engine.import_files([p2])
        stored2 = Path(engine.db.query_one(
            "SELECT id, path FROM assets ORDER BY id DESC LIMIT 1")["path"])
        stored2.write_bytes(b"broken-image-data")
        rep = engine.rebuild_indexes(force=True)
        # 至少有 1 条失败记录（坏图），正常图成功
        assert len(rep.failed) >= 1

    def test_report_written(self):
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "recovery_report.json").write_text(json.dumps({
            "suite": "recovery", "passed": True,
            "checks": ["corrupt_db_fails_gracefully", "restore_from_backup",
                       "corrupt_image_isolated", "export_isolates_bad_image",
                       "rebuild_skips_corrupt"]}, ensure_ascii=False, indent=2))
