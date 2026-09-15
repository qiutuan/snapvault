"""数据库 Schema 与版本化迁移。

版本管理使用 PRAGMA user_version；每次迁移在一个事务中执行。
升级失败时事务回滚，数据库停留在旧版本，可安全重试。
"""
from __future__ import annotations

import sqlite3
from typing import Callable, List

SCHEMA_VERSION = 1
EMBEDDING_DIM = 512  # bge-small-zh-v1.5 输出维度


def _migration_v1(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        -- 截图主表
        CREATE TABLE assets (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            path          TEXT NOT NULL UNIQUE,        -- 数据目录内实际存储路径
            original_path TEXT,                        -- 导入时的原始路径
            sha256        TEXT NOT NULL,
            width         INTEGER NOT NULL DEFAULT 0,
            height        INTEGER NOT NULL DEFAULT 0,
            format        TEXT,
            size_bytes    INTEGER NOT NULL DEFAULT 0,
            source        TEXT NOT NULL DEFAULT 'import', -- clipboard|capture|import|dragdrop
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            imported_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            ocr_status    TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|skipped
            ocr_confidence REAL,
            deleted_at    TEXT,                        -- 软删除（回收站）
            deleted_reason TEXT
        );
        CREATE INDEX idx_assets_sha256    ON assets(sha256);
        CREATE INDEX idx_assets_deleted   ON assets(deleted_at);
        CREATE INDEX idx_assets_ocrstatus ON assets(ocr_status);
        CREATE INDEX idx_assets_created   ON assets(created_at);

        -- OCR 结果分块
        CREATE TABLE ocr_texts (
            id          INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id    INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            chunk_index INTEGER NOT NULL,
            text        TEXT NOT NULL,
            confidence  REAL,
            bbox        TEXT,           -- JSON [x0,y0,x1,y1,...]
            lang        TEXT,
            UNIQUE (asset_id, chunk_index)
        );

        -- FTS5 全文索引（trigram 分词：中文子串可检索）
        CREATE VIRTUAL TABLE ocr_fts USING fts5(
            asset_id UNINDEXED,
            chunk_index UNINDEXED,
            text,
            tokenize = 'trigram'
        );

        -- 标签（层级用 / 分隔，如 项目A/登录页）
        CREATE TABLE tags (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            name       TEXT NOT NULL UNIQUE,
            color      TEXT,
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE asset_tags (
            asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            tag_id   INTEGER NOT NULL REFERENCES tags(id) ON DELETE CASCADE,
            PRIMARY KEY (asset_id, tag_id)
        );
        CREATE INDEX idx_asset_tags_tag ON asset_tags(tag_id);

        -- Markdown 备注
        CREATE TABLE notes (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id   INTEGER NOT NULL UNIQUE REFERENCES assets(id) ON DELETE CASCADE,
            content    TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        -- 备注全文（可配置开关）
        CREATE VIRTUAL TABLE notes_fts USING fts5(
            asset_id UNINDEXED,
            content,
            tokenize = 'trigram'
        );

        -- 向量（sqlite-vec）：普通表存原始 blob 便于备份/重建，vec0 提供 ANN 索引
        CREATE TABLE embeddings (
            asset_id  INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
            embedding BLOB NOT NULL,
            model     TEXT NOT NULL,
            dim       INTEGER NOT NULL
        );
        CREATE VIRTUAL TABLE vec_embeddings USING vec0(embedding float[512]);

        -- 异步任务队列
        CREATE TABLE jobs (
            id            INTEGER PRIMARY KEY AUTOINCREMENT,
            asset_id      INTEGER REFERENCES assets(id) ON DELETE CASCADE,
            type          TEXT NOT NULL,            -- ocr|embed|thumb|import
            status        TEXT NOT NULL DEFAULT 'pending', -- pending|running|done|failed|skipped
            attempts      INTEGER NOT NULL DEFAULT 0,
            max_attempts  INTEGER NOT NULL DEFAULT 3,
            next_retry_at TEXT,
            error         TEXT,
            created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            started_at    TEXT,
            finished_at   TEXT,
            priority      INTEGER NOT NULL DEFAULT 0
        );
        CREATE INDEX idx_jobs_status ON jobs(status, priority);

        -- 自定义字段定义
        CREATE TABLE custom_field_defs (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            key        TEXT NOT NULL UNIQUE,
            label      TEXT NOT NULL,
            type       TEXT NOT NULL,               -- text|single_choice|date
            options    TEXT,                        -- JSON 数组（single_choice）
            created_at TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );
        CREATE TABLE custom_field_values (
            asset_id INTEGER NOT NULL REFERENCES assets(id) ON DELETE CASCADE,
            field_id INTEGER NOT NULL REFERENCES custom_field_defs(id) ON DELETE CASCADE,
            value    TEXT,
            PRIMARY KEY (asset_id, field_id)
        );

        -- 标注图层（JSON）
        CREATE TABLE annotations (
            asset_id    INTEGER PRIMARY KEY REFERENCES assets(id) ON DELETE CASCADE,
            layers_json TEXT NOT NULL DEFAULT '[]',
            updated_at  TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
        );

        -- 应用设置
        CREATE TABLE settings (
            key   TEXT PRIMARY KEY,
            value TEXT
        );

        -- 审计日志（关键操作留痕）
        CREATE TABLE audit_log (
            id       INTEGER PRIMARY KEY AUTOINCREMENT,
            ts       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
            action   TEXT NOT NULL,
            detail   TEXT,
            asset_id INTEGER
        );
        """
    )


MIGRATIONS: List[Callable[[sqlite3.Connection], None]] = [
    _migration_v1,
]


def migrate(conn: sqlite3.Connection) -> None:
    """将数据库迁移到最新版本。每个迁移独立事务，失败即回滚。"""
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > SCHEMA_VERSION:
        raise ValueError(
            f"数据库版本({current})高于程序支持的版本({SCHEMA_VERSION})，请升级程序"
        )
    for target in range(current + 1, SCHEMA_VERSION + 1):
        with conn:
            MIGRATIONS[target - 1](conn)
            conn.execute(f"PRAGMA user_version = {target}")


def integrity_check(conn: sqlite3.Connection) -> list[str]:
    """PRAGMA integrity_check；返回空列表表示通过。"""
    rows = conn.execute("PRAGMA integrity_check").fetchall()
    return [r[0] for r in rows if r[0] != "ok"]
