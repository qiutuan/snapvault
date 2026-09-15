"""备份与迁移（M9）：定时快照 / 库导出导入。

- 快照：SQLite 在线备份 API（WAL 安全一致）+ 图片清单 manifest，保留最近 N 份
- 库导出：zip 打包「数据库 + 原图 + 配置」，可整体迁移
- 库导入：解包到新数据根目录并做完整性校验
"""
from __future__ import annotations

import json
import logging
import shutil
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Config
from .db import Database, open_database
from .exceptions import DatabaseError
from .logging_setup import log_event
from .timeutil import utc_now
from .util import atomic_write_text, sha256_file

logger = logging.getLogger("snapvault.backup")

BACKUP_MARKER = "SnapVault library export v1"


@dataclass
class BackupInfo:
    path: str
    created_at: str
    asset_count: int
    size_bytes: int
    encrypted: bool = False


class BackupManager:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config

    # ==================================================================
    # 快照
    # ==================================================================
    def create_snapshot(self, encrypt_passphrase: str | None = None) -> BackupInfo:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        snap_dir = self.config.backups_dir() / f"snapshot_{stamp}"
        snap_dir.mkdir(parents=True, exist_ok=True)

        db_file = snap_dir / "snapvault.db"
        # SQLite 在线备份：跨连接复制一致快照（WAL 安全）
        src_conn = self.db._conn()
        with open_database(db_file) as dst_conn:
            src_conn.backup(dst_conn)
        # WAL 收尾（确保独立文件可读）
        dst = open_database(db_file)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst.close()

        asset_count = int(self.db.scalar("SELECT COUNT(*) FROM assets") or 0)
        files = []
        for row in self.db.query("SELECT path, sha256, size_bytes FROM assets"):
            files.append({
                "path": row["path"],
                "sha256": row["sha256"],
                "size": row["size_bytes"],
            })
        manifest = {
            "type": "snapshot",
            "created_at": utc_now(),
            "asset_count": asset_count,
            "app_version": "0.1.0",
            "files": files,
        }
        mpath = snap_dir / "manifest.json"
        atomic_write_text(mpath, json.dumps(manifest, ensure_ascii=False, indent=2))

        if encrypt_passphrase:
            from .crypto import encrypt_file

            enc = snap_dir / "snapvault.db.enc"
            encrypt_file(db_file, enc, encrypt_passphrase)
            db_file.unlink()
            mpath.unlink()
            db_file = enc
            encrypted = True
        else:
            encrypted = False

        size = sum(p.stat().st_size for p in snap_dir.rglob("*") if p.is_file())
        info = BackupInfo(
            path=str(snap_dir), created_at=stamp, asset_count=asset_count,
            size_bytes=size, encrypted=encrypted,
        )
        self._prune()
        log_event(logger, "backup.created", path=str(snap_dir), count=asset_count,
                  encrypted=encrypted)
        return info

    def _prune(self) -> None:
        """保留最近 keep 份快照（默认 7）。"""
        keep = max(1, self.config.backup_keep)
        snaps = sorted(
            (p for p in self.config.backups_dir().glob("snapshot_*") if p.is_dir()),
            key=lambda p: p.name, reverse=True,
        )
        for old in snaps[keep:]:
            shutil.rmtree(old, ignore_errors=True)
            log_event(logger, "backup.pruned", path=str(old))

    def list_snapshots(self) -> list[BackupInfo]:
        out = []
        for d in sorted(self.config.backups_dir().glob("snapshot_*"), reverse=True):
            if not d.is_dir():
                continue
            try:
                manifest = json.loads((d / "manifest.json").read_text("utf-8"))
            except (OSError, json.JSONDecodeError):
                manifest = {}
            db_file = d / ("snapvault.db.enc" if (d / "snapvault.db.enc").exists()
                           else "snapvault.db")
            out.append(BackupInfo(
                path=str(d), created_at=d.name.replace("snapshot_", ""),
                asset_count=int(manifest.get("asset_count", -1)),
                size_bytes=db_file.stat().st_size if db_file.exists() else 0,
                encrypted=db_file.suffix == ".enc",
            ))
        return out

    def restore_snapshot(self, snapshot_path: str | Path,
                         decrypt_passphrase: str | None = None) -> int:
        """用快照数据库覆盖当前库（先备份当前库为 .pre_restore）。"""
        snap = Path(snapshot_path)
        db_file = snap / "snapvault.db.enc" if (snap / "snapvault.db.enc").exists() \
            else snap / "snapvault.db"
        if not db_file.exists():
            raise FileNotFoundError(f"快照中无数据库: {snap}")
        if db_file.suffix == ".enc":
            if not decrypt_passphrase:
                raise ValueError("该快照已加密，需要口令")
            from .crypto import decrypt_file

            tmp = snap / ".restore_tmp.db"
            decrypt_file(db_file, tmp, decrypt_passphrase)
            db_file = tmp

        # 恢复前备份当前库
        pre = self.config.db_path().with_suffix(".pre_restore.db")
        src = self.db._conn()
        with open_database(pre) as dst:
            src.backup(dst)
        dst = open_database(pre)
        dst.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        dst.close()

        target = self.config.db_path()
        self.db.close()
        shutil.copyfile(db_file, target)
        # 删除旧 WAL/SHM，避免陈旧日志
        for suffix in ("-wal", "-shm"):
            p = Path(str(target) + suffix)
            if p.exists():
                p.unlink()
        # 校验并重开
        problems = open_database(target)
        problems.close()
        if not Database(target).integrity_ok():
            raise DatabaseError("恢复后的数据库完整性校验失败")
        # 重开新连接
        count = int(self.db.scalar("SELECT COUNT(*) FROM assets") or 0)
        log_event(logger, "backup.restored", path=str(snap), assets=count)
        return count

    # ==================================================================
    # 整库导出 / 导入（迁移）
    # ==================================================================
    def export_library(self, dst: str | Path | None = None,
                       on_progress: Callable[[int, int], None] | None = None) -> str:
        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
        dst = Path(dst) if dst else self.config.exports_dir() / f"snapvault_library_{stamp}.zip"
        dst.parent.mkdir(parents=True, exist_ok=True)

        info = self.create_snapshot()
        snap = Path(info.path)
        zip_path = Path(str(dst))
        total = 0
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
            zf.writestr(".snapvault_marker", BACKUP_MARKER)
            db_file = snap / "snapvault.db"
            if db_file.exists():
                zf.write(db_file, "db/snapvault.db")
                total += 1
            mfile = snap / "manifest.json"
            if mfile.exists():
                zf.write(mfile, "db/manifest.json")
                total += 1
            if (self.config.root / "config.json").exists():
                zf.write(self.config.root / "config.json", "config.json")
            images = sorted(self.config.images_dir().glob("*"))
            for i, img in enumerate(images, start=1):
                if img.is_file():
                    zf.write(img, f"images/{img.name}")
                    total += 1
                if on_progress:
                    on_progress(i, len(images) or 1)
        log_event(logger, "library.exported", path=str(zip_path), files=total)
        return str(zip_path)

    def import_library(self, zip_path: str | Path, target_root: str | Path | None = None,
                       restore_db: bool = True) -> Path:
        """导入迁移包到（新）数据根目录。返回目标根目录。"""
        zip_path = Path(zip_path)
        try:
            with zipfile.ZipFile(zip_path) as zf:
                marker = zf.read(".snapvault_marker").decode("utf-8").strip()
                if marker != BACKUP_MARKER:
                    raise ValueError("不是 SnapVault 库导出包")
                target = Path(target_root) if target_root else \
                    Path(zip_path).with_suffix("")  # 解包到同名目录
                target.mkdir(parents=True, exist_ok=True)
                for name in zf.namelist():
                    if name.startswith(("db/", "images/", "config.json")):
                        zf.extract(name, target)
        except KeyError as exc:
            raise ValueError("不是 SnapVault 库导出包（缺少标记）") from exc
        if restore_db and (target / "db" / "snapvault.db").exists():
            open_database(target / "db" / "snapvault.db").close()
            if not Database(target / "db" / "snapvault.db").integrity_ok():
                raise DatabaseError("导入的数据库完整性校验失败")
        log_event(logger, "library.imported", zip=str(zip_path), target=str(target))
        return target
