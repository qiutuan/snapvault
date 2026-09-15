"""M9 备份测试：快照一致性 / 保留份数 / 恢复 / 整库导出导入 / 加密快照。"""
from __future__ import annotations

from pathlib import Path

import pytest

from snapvault.backup import BackupManager
from snapvault.importer import Importer


@pytest.fixture()
def bm(db, config):
    return BackupManager(db, config)


@pytest.fixture()
def seeded(db, config, make_image):
    img = make_image("备份测试 123")
    aid = Importer(db, config).import_files([img]).imported[0]
    return aid


class TestSnapshot:
    def test_create_and_list(self, bm, seeded):
        info = bm.create_snapshot()
        assert info.asset_count == 1
        assert (Path(info.path) / "snapvault.db").exists()
        assert (Path(info.path) / "manifest.json").exists()
        snaps = bm.list_snapshots()
        assert len(snaps) == 1 and snaps[0].asset_count == 1

    def test_snapshot_db_usable(self, bm, seeded, db):
        from snapvault.db import open_database

        info = bm.create_snapshot()
        conn = open_database(Path(info.path) / "snapvault.db")
        try:
            assert conn.execute("SELECT COUNT(*) FROM assets").fetchone()[0] == 1
            assert conn.execute("PRAGMA integrity_check").fetchone()[0] == "ok"
        finally:
            conn.close()

    def test_prune_keeps_recent(self, bm, seeded, config):
        for _ in range(9):
            bm.create_snapshot()
        snaps = bm.list_snapshots()
        assert len(snaps) <= config.backup_keep  # 默认 7

    def test_restore(self, bm, seeded, db, config):
        info = bm.create_snapshot()
        # 破坏当前库后恢复
        db.execute("DELETE FROM assets")
        assert db.scalar("SELECT COUNT(*) FROM assets") == 0
        n = bm.restore_snapshot(info.path)
        assert n == 1
        assert db.scalar("SELECT COUNT(*) FROM assets") == 1

    def test_encrypted_snapshot(self, bm, seeded):
        info = bm.create_snapshot(encrypt_passphrase="secret")
        assert info.encrypted is True
        assert (Path(info.path) / "snapvault.db.enc").exists()
        assert not (Path(info.path) / "snapvault.db").exists()

    def test_restore_encrypted_requires_password(self, bm, seeded, db):
        info = bm.create_snapshot(encrypt_passphrase="pw")
        with pytest.raises(ValueError, match="口令"):
            bm.restore_snapshot(info.path)
        n = bm.restore_snapshot(info.path, decrypt_passphrase="pw")
        assert n == 1

    def test_restore_wrong_password(self, bm, seeded, db):
        from snapvault.crypto import decrypt_file

        info = bm.create_snapshot(encrypt_passphrase="pw")
        from snapvault.exceptions import CryptoError

        with pytest.raises(CryptoError):
            bm.restore_snapshot(info.path, decrypt_passphrase="wrong")


class TestLibraryTransfer:
    def test_export_import_roundtrip(self, bm, seeded, db, config, make_image, tmp_path):
        from snapvault.metadata import MetadataService

        ms = MetadataService(db, config)
        ms.set_asset_tags(seeded, ["迁移/测试"])
        ms.set_note(seeded, "迁移备注")
        zip_path = bm.export_library()
        assert Path(zip_path).exists() and zip_path.endswith(".zip")
        target = bm.import_library(zip_path, target_root=tmp_path / "new_root")
        assert (target / "db" / "snapvault.db").exists()
        assert (target / "images").exists()
        from snapvault.db import Database

        ndb = Database(target / "db" / "snapvault.db")
        assert ndb.scalar("SELECT COUNT(*) FROM assets") == 1
        assert ndb.scalar("SELECT COUNT(*) FROM tags WHERE name='迁移/测试'") == 1
        assert ndb.scalar("SELECT content FROM notes WHERE asset_id=?", (seeded,)) == "迁移备注"

    def test_import_rejects_non_library(self, tmp_path):
        bogus = tmp_path / "bogus.zip"
        import zipfile

        with zipfile.ZipFile(bogus, "w") as zf:
            zf.writestr("random.txt", "hi")
        from snapvault.backup import BackupManager
        from snapvault.config import Config
        from snapvault.db import Database

        bm = BackupManager(Database(tmp_path / "x" / "t.db"), Config(tmp_path / "y"))
        with pytest.raises(ValueError, match="不是"):
            bm.import_library(bogus)
