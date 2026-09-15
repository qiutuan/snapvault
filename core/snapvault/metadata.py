"""元数据管理（M5）：多标签体系 / Markdown 备注（进全文索引）/ 自定义字段 / 回收站。"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .db import Database
from .logging_setup import log_event

logger = logging.getLogger("snapvault.metadata")


@dataclass
class Tag:
    id: int
    name: str
    color: str | None


@dataclass
class FieldDef:
    id: int
    key: str
    label: str
    type: str
    options: list[str]


class MetadataService:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ==================================================================
    # 标签
    # ==================================================================
    def list_tags(self) -> list[Tag]:
        rows = self.db.query("SELECT id, name, color FROM tags ORDER BY name")
        return [Tag(r["id"], r["name"], r["color"]) for r in rows]

    def create_tag(self, name: str, color: str | None = None) -> int:
        name = name.strip().strip("/")
        if not name:
            raise ValueError("标签名不能为空")
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO tags(name, color) VALUES (?, ?) RETURNING id",
                (name, color),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"标签已存在: {name}")
            conn.execute("INSERT INTO audit_log(action, detail) VALUES ('tag.create', ?)",
                         (name,))
        return row[0]

    def rename_tag(self, old_name: str, new_name: str) -> None:
        new_name = new_name.strip().strip("/")
        if not new_name:
            raise ValueError("标签名不能为空")
        with self.db.tx() as conn:
            cur = conn.execute(
                "UPDATE tags SET name=? WHERE name=? RETURNING id", (new_name, old_name)
            )
            if cur.fetchone() is None:
                raise ValueError(f"标签不存在: {old_name}")
            conn.execute("INSERT INTO audit_log(action, detail) VALUES ('tag.rename', ?)",
                         (f"{old_name} -> {new_name}",))

    def delete_tag(self, name: str) -> None:
        with self.db.tx() as conn:
            cur = conn.execute("DELETE FROM tags WHERE name=? RETURNING id", (name,))
            if cur.fetchone() is None:
                raise ValueError(f"标签不存在: {name}")
            conn.execute("INSERT INTO audit_log(action, detail) VALUES ('tag.delete', ?)",
                         (name,))

    def set_asset_tags(self, asset_id: int, tags: list[str]) -> list[str]:
        """全量同步某资产的标签；返回最终标签列表。"""
        clean = []
        seen = set()
        for t in tags:
            t = t.strip().strip("/")
            if t and t not in seen:
                clean.append(t)
                seen.add(t)
        with self.db.tx() as conn:
            conn.execute("DELETE FROM asset_tags WHERE asset_id=?", (asset_id,))
            for name in clean:
                conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (name,))
                tid = conn.execute("SELECT id FROM tags WHERE name=?", (name,)).fetchone()[0]
                conn.execute(
                    "INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES (?, ?)",
                    (asset_id, tid),
                )
        return clean

    def get_asset_tags(self, asset_id: int) -> list[str]:
        rows = self.db.query(
            "SELECT t.name FROM asset_tags at JOIN tags t ON t.id = at.tag_id "
            "WHERE at.asset_id=? ORDER BY t.name",
            (asset_id,),
        )
        return [r["name"] for r in rows]

    def tag_tree(self) -> list[dict]:
        """按 / 分层构造标签树。"""
        names = sorted(t.name for t in self.list_tags())
        root: list[dict] = []

        def _node(label: str) -> dict:
            return {"label": label, "path": label, "children": []}

        for name in names:
            parts = name.split("/")
            level = root
            path = ""
            for i, part in enumerate(parts):
                path = f"{path}/{part}" if path else part
                node = next((n for n in level if n["path"] == path), None)
                if node is None:
                    node = _node(part)
                    node["path"] = path
                    node["full"] = path if i == len(parts) - 1 else None
                    level.append(node)
                level = node["children"]
        return root

    # ==================================================================
    # 备注（Markdown，进全文索引，可配置）
    # ==================================================================
    def set_note(self, asset_id: int, content: str) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "INSERT INTO notes(asset_id, content, updated_at) VALUES (?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET "
                "content=excluded.content, updated_at=excluded.updated_at",
                (asset_id, content, datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")),
            )
            conn.execute("DELETE FROM notes_fts WHERE asset_id=?", (asset_id,))
            if content.strip():
                conn.execute("INSERT INTO notes_fts(asset_id, content) VALUES (?, ?)",
                             (asset_id, content))

    def get_note(self, asset_id: int) -> str:
        return self.db.scalar("SELECT content FROM notes WHERE asset_id=?", (asset_id,)) or ""

    def delete_note(self, asset_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM notes_fts WHERE asset_id=?", (asset_id,))
            conn.execute("DELETE FROM notes WHERE asset_id=?", (asset_id,))

    # ==================================================================
    # 自定义字段
    # ==================================================================
    def define_field(self, key: str, label: str, ftype: str = "text",
                     options: list[str] | None = None) -> int:
        if ftype not in ("text", "single_choice", "date"):
            raise ValueError(f"不支持的类型: {ftype}")
        key = key.strip()
        if not key:
            raise ValueError("字段 key 不能为空")
        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT OR IGNORE INTO custom_field_defs(key, label, type, options) "
                "VALUES (?, ?, ?, ?) RETURNING id",
                (key, label or key, ftype, json.dumps(options or [], ensure_ascii=False)),
            )
            row = cur.fetchone()
            if row is None:
                raise ValueError(f"字段已存在: {key}")
        return row[0]

    def list_fields(self) -> list[FieldDef]:
        rows = self.db.query("SELECT * FROM custom_field_defs ORDER BY id")
        out = []
        for r in rows:
            try:
                opts = json.loads(r["options"] or "[]")
            except json.JSONDecodeError:
                opts = []
            out.append(FieldDef(r["id"], r["key"], r["label"], r["type"], opts))
        return out

    def delete_field(self, field_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute("DELETE FROM custom_field_values WHERE field_id=?", (field_id,))
            conn.execute("DELETE FROM custom_field_defs WHERE id=?", (field_id,))

    def set_field_value(self, asset_id: int, key: str, value: str | None) -> None:
        fid = self.db.scalar("SELECT id FROM custom_field_defs WHERE key=?", (key,))
        if fid is None:
            raise ValueError(f"字段未定义: {key}")
        with self.db.tx() as conn:
            if value is None:
                conn.execute(
                    "DELETE FROM custom_field_values WHERE asset_id=? AND field_id=?",
                    (asset_id, fid),
                )
            else:
                conn.execute(
                    "INSERT INTO custom_field_values(asset_id, field_id, value) "
                    "VALUES (?, ?, ?) ON CONFLICT(asset_id, field_id) DO UPDATE SET "
                    "value=excluded.value",
                    (asset_id, fid, value),
                )

    def get_field_values(self, asset_id: int) -> dict[str, str]:
        rows = self.db.query(
            "SELECT cfd.key, cfv.value FROM custom_field_values cfv "
            "JOIN custom_field_defs cfd ON cfd.id = cfv.field_id "
            "WHERE cfv.asset_id=?",
            (asset_id,),
        )
        return {r["key"]: r["value"] for r in rows}

    # ==================================================================
    # 回收站（软删除，30 天可恢复）
    # ==================================================================
    def trash(self, asset_id: int, reason: str = "manual") -> None:
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%fZ")
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE assets SET deleted_at=?, deleted_reason=? WHERE id=? AND deleted_at IS NULL",
                (now, reason, asset_id),
            )
            conn.execute("INSERT INTO audit_log(action, detail, asset_id) "
                         "VALUES ('trash', ?, ?)", (reason, asset_id))

    def restore(self, asset_id: int) -> None:
        with self.db.tx() as conn:
            conn.execute(
                "UPDATE assets SET deleted_at=NULL, deleted_reason=NULL "
                "WHERE id=? AND deleted_at IS NOT NULL",
                (asset_id,),
            )
            conn.execute("INSERT INTO audit_log(action, detail, asset_id) "
                         "VALUES ('restore', 'from_trash', ?)", (asset_id,))

    def purge(self, asset_id: int) -> None:
        """彻底删除：删除图片文件 + 数据库行（级联清理所有关联数据）。"""
        import os

        asset = self.db.query_one("SELECT path FROM assets WHERE id=?", (asset_id,))
        if asset is None:
            return
        with self.db.tx() as conn:
            conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
            conn.execute("INSERT INTO audit_log(action, detail, asset_id) "
                         "VALUES ('purge', 'permanent', ?)", (asset_id,))
        for p in (Path(asset["path"]), self.config.thumbnails_dir() / f"{asset_id}.jpg"):
            try:
                if p.exists():
                    os.remove(p)
            except OSError:
                logger.warning("purge 删除文件失败: %s", p)

    def list_trashed(self) -> list[dict]:
        rows = self.db.query(
            "SELECT id, path, deleted_at, deleted_reason FROM assets "
            "WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC"
        )
        return [dict(r) for r in rows]

    def cleanup_expired(self) -> int:
        """清除超过保留期（默认 30 天）的回收站记录，返回清除数量。"""
        from pathlib import Path

        cutoff = (datetime.now(timezone.utc) - timedelta(days=self.config.recycle_bin_days)
                  ).strftime("%Y-%m-%dT%H:%M:%fZ")
        rows = self.db.query(
            "SELECT id, path FROM assets WHERE deleted_at IS NOT NULL AND deleted_at < ?",
            (cutoff,),
        )
        for r in rows:
            self.purge(r["id"])
        if rows:
            log_event(logger, "recycle.cleanup", count=len(rows))
        return len(rows)
