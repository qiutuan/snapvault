"""M1 数据层测试：WAL / 外键 / 事务 / 迁移 / 完整性 / sqlite-vec / FTS5。"""
from __future__ import annotations

import sqlite3

import pytest

from snapvault.db import Database, open_database
from snapvault.exceptions import DatabaseError
from snapvault.schema import SCHEMA_VERSION, migrate


class TestOpenDatabase:
    def test_creates_schema_and_wal(self, db_path):
        conn = open_database(db_path)
        try:
            assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
            assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            for t in ("assets", "ocr_texts", "ocr_fts", "tags", "asset_tags",
                      "notes", "notes_fts", "embeddings", "vec_embeddings",
                      "jobs", "custom_field_defs", "custom_field_values",
                      "annotations", "settings", "audit_log"):
                assert t in tables, f"缺少表 {t}"
        finally:
            conn.close()

    def test_wal_files_created(self, db_path):
        open_database(db_path).close()
        assert (db_path.with_name(db_path.name + "-wal") is not None)
        assert db_path.exists()

    def test_migrate_idempotent(self, db_path):
        open_database(db_path).close()
        conn = open_database(db_path)  # 再次打开不重复迁移
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()

    def test_migrate_forward(self, db_path):
        """旧版本数据库可升级到最新。"""
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute("CREATE TABLE legacy (id INTEGER PRIMARY KEY)")  # 旧版遗留表（不冲突）
        conn.commit()
        conn.close()
        migrate(open_database(db_path))
        conn = open_database(db_path)
        try:
            assert conn.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        finally:
            conn.close()

    def test_future_version_rejected(self, db_path):
        db_path.parent.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(str(db_path))
        conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
        conn.commit()
        conn.close()
        with pytest.raises(ValueError, match="高于"):
            open_database(db_path)


class TestTransactions:
    def test_commit(self, db):
        with db.tx() as conn:
            conn.execute("INSERT INTO settings(key, value) VALUES ('k', 'v')")
        assert db.scalar("SELECT value FROM settings WHERE key='k'") == "v"

    def test_rollback_on_error(self, db):
        with pytest.raises(RuntimeError):
            with db.tx() as conn:
                conn.execute("INSERT INTO settings(key, value) VALUES ('k', 'v')")
                raise RuntimeError("boom")
        assert db.scalar("SELECT value FROM settings WHERE key='k'") is None

    def test_multi_step_atomic(self, db):
        """多步写入在同一事务：中途失败则全部回滚。"""
        with pytest.raises(sqlite3.IntegrityError):
            with db.tx() as conn:
                conn.execute("INSERT INTO settings(key, value) VALUES ('a', '1')")
                conn.execute("INSERT INTO assets(path, sha256) VALUES ('/x', 'h1')")
                conn.execute("INSERT INTO assets(path, sha256) VALUES ('/x', 'h2')")  # 唯一冲突
        assert db.scalar("SELECT COUNT(*) FROM settings WHERE key='a'") == 0
        assert db.scalar("SELECT COUNT(*) FROM assets") == 0


class TestForeignKeys:
    def test_cascade_delete(self, db):
        asset_id = db.execute(
            "INSERT INTO assets(path, sha256) VALUES (?, ?) RETURNING id",
            ("/a.png", "abc"),
        ).fetchone()[0]
        db.execute("INSERT INTO ocr_texts(asset_id, chunk_index, text) VALUES (?, 0, 'hi')",
                   (asset_id,))
        db.execute("INSERT INTO tags(name) VALUES ('t')")
        tag_id = db.scalar("SELECT id FROM tags WHERE name='t'")
        db.execute("INSERT INTO asset_tags(asset_id, tag_id) VALUES (?, ?)", (asset_id, tag_id))
        db.execute("INSERT INTO notes(asset_id, content) VALUES (?, 'note')", (asset_id,))
        db.execute("DELETE FROM assets WHERE id=?", (asset_id,))
        assert db.scalar("SELECT COUNT(*) FROM ocr_texts WHERE asset_id=?", (asset_id,)) == 0
        assert db.scalar("SELECT COUNT(*) FROM asset_tags WHERE asset_id=?", (asset_id,)) == 0
        assert db.scalar("SELECT COUNT(*) FROM notes WHERE asset_id=?", (asset_id,)) == 0


class TestIntegrity:
    def test_corruption_detected(self, db_path):
        db = Database(db_path)
        db.execute("INSERT INTO settings(key, value) VALUES ('x', '1')")
        db.close()
        # 破坏 SQLite 文件头魔数（确定性损坏）
        data = bytearray(db_path.read_bytes())
        data[15] ^= 0xFF
        db_path.write_bytes(bytes(data))
        with pytest.raises(DatabaseError):
            open_database(db_path)

    def test_integrity_ok(self, db):
        assert db.integrity_ok() is True


class TestSqliteVec:
    def test_vector_insert_and_search(self, db):
        import struct

        def vec(vals):
            return struct.pack("<%df" % len(vals), *vals)

        with db.tx() as conn:
            for i in range(5):
                aid = conn.execute(
                    "INSERT INTO assets(path, sha256) VALUES (?, ?) RETURNING id",
                    (f"/{i}.png", f"h{i}"),
                ).fetchone()[0]
                conn.execute(
                    "INSERT INTO embeddings(asset_id, embedding, model, dim) VALUES (?,?,?,?)",
                    (aid, vec([float(i)] * 512), "bge-small-zh-v1.5", 512),
                )
                conn.execute(
                    "INSERT INTO vec_embeddings(rowid, embedding) VALUES (?, ?)",
                    (aid, vec([float(i)] * 512)),
                )
        rows = db.query(
            "SELECT rowid, distance FROM vec_embeddings "
            "WHERE embedding MATCH ? AND k = 3",
            (vec([4.1] * 512),),
        )
        assert rows[0]["rowid"] == 5  # 最近邻：asset id=5 即 i=4（[4.0]*512）
        assert len(rows) == 3


class TestFtsTrigram:
    def test_chinese_substring(self, db):
        with db.tx() as conn:
            aid = conn.execute(
                "INSERT INTO assets(path, sha256) VALUES ('/a.png', 'h') RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO ocr_texts(asset_id, chunk_index, text) VALUES (?, 0, ?)",
                (aid, "登录页面加载失败 服务器返回500"),
            )
            conn.execute(
                "INSERT INTO ocr_fts(asset_id, chunk_index, text) VALUES (?, 0, ?)",
                (aid, "登录页面加载失败 服务器返回500"),
            )
        hits = db.query(
            "SELECT asset_id, bm25(ocr_fts) AS score FROM ocr_fts "
            "WHERE ocr_fts MATCH ? ORDER BY score",
            ("登录页面",),
        )
        assert len(hits) == 1
        assert hits[0]["asset_id"] == aid

    def test_english_word(self, db):
        with db.tx() as conn:
            aid = conn.execute(
                "INSERT INTO assets(path, sha256) VALUES ('/b.png', 'h2') RETURNING id"
            ).fetchone()[0]
            conn.execute(
                "INSERT INTO ocr_fts(asset_id, chunk_index, text) VALUES (?, 0, ?)",
                (aid, "timeout connecting to redis cluster"),
            )
        hits = db.query(
            "SELECT asset_id FROM ocr_fts WHERE ocr_fts MATCH ?", ("timeout",))
        assert len(hits) == 1
