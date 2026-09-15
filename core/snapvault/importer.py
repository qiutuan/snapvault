"""导入管线（M3）：拖拽/目录批量导入、SHA-256 去重、原子落盘、任务入队。

- 落盘前计算 SHA-256：重复图片自动合并（保留既有标签备注关联），软删除的自动恢复
- 原图只读保存于数据目录 images/，写入采用原子写（临时文件+rename）
- 图片永不修改：仅做校验与缩略图（缩略图另存）
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .config import Config
from .db import Database
from .jobs import JobManager
from .logging_setup import log_event
from .naming import unique_filename
from .util import atomic_write, is_supported_image, sha256_file

logger = logging.getLogger("snapvault.importer")


@dataclass
class ImportReport:
    imported: list[int] = field(default_factory=list)      # 新增 asset id
    duplicates: list[str] = field(default_factory=list)    # 重复（已合并）原始路径
    restored: list[int] = field(default_factory=list)      # 从回收站恢复的 asset id
    failed: list[tuple[str, str]] = field(default_factory=list)  # (路径, 错误)
    skipped_ext: list[str] = field(default_factory=list)

    @property
    def total(self) -> int:
        return len(self.imported) + len(self.duplicates) + len(self.restored) + len(self.failed)


def validate_image(path: str | Path) -> tuple[int, int, str]:
    """校验图片可读，返回 (宽, 高, 格式)。损坏图片抛异常。"""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(path) as img:
            img.verify()
        with Image.open(path) as img:
            w, h = img.size
            fmt = (img.format or "UNKNOWN").upper()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        raise ValueError(f"不是可读图片: {exc}") from exc
    if w <= 0 or h <= 0:
        raise ValueError("图片尺寸异常")
    return w, h, fmt


class Importer:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.jobs = JobManager(db)

    # ------------------------------------------------------------------
    def import_files(
        self,
        paths: list[str | Path],
        source: str = "import",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ImportReport:
        """批量导入文件。相同 SHA-256 自动去重合并。"""
        report = ImportReport()
        total = len(paths)
        for idx, raw in enumerate(paths, start=1):
            path = Path(raw)
            if not path.is_file():
                report.failed.append((str(path), "文件不存在"))
                continue
            if not is_supported_image(path):
                report.skipped_ext.append(str(path))
                continue
            try:
                self._import_one(path, source, report)
            except Exception as exc:  # noqa: BLE001
                report.failed.append((str(path), str(exc)))
            if on_progress:
                on_progress(idx, total)
        log_event(
            logger, "import.batch",
            count=len(report.imported), duplicates=len(report.duplicates),
            restored=len(report.restored), failed=len(report.failed),
        )
        return report

    # ------------------------------------------------------------------
    def import_directory(
        self,
        dir_path: str | Path,
        recursive: bool = True,
        source: str = "import",
        on_progress: Callable[[int, int], None] | None = None,
    ) -> ImportReport:
        """整个目录批量导入（增量：跳过已存在哈希）。"""
        base = Path(dir_path)
        if not base.is_dir():
            raise ValueError(f"目录不存在: {base}")
        iterator = base.rglob("*") if recursive else base.glob("*")
        paths = [p for p in iterator if p.is_file() and is_supported_image(p)]
        paths.sort(key=lambda p: str(p).lower())
        return self.import_files(paths, source=source, on_progress=on_progress)

    # ------------------------------------------------------------------
    def _import_one(self, path: Path, source: str, report: ImportReport) -> None:
        sha = sha256_file(path)

        # 1) 存在未删除的重复资产 → 合并（保留既有标签/备注）
        existing = self.db.query_one(
            "SELECT id FROM assets WHERE sha256=? AND deleted_at IS NULL",
            (sha,),
        )
        if existing:
            report.duplicates.append(str(path))
            log_event(logger, "import.duplicate", asset_id=existing["id"], sha256=sha[:16])
            return

        # 2) 回收站中有同哈希 → 恢复
        trashed = self.db.query_one(
            "SELECT id FROM assets WHERE sha256=? AND deleted_at IS NOT NULL "
            "ORDER BY deleted_at DESC LIMIT 1",
            (sha,),
        )
        if trashed:
            with self.db.tx() as conn:
                conn.execute(
                    "UPDATE assets SET deleted_at=NULL, deleted_reason=NULL WHERE id=?",
                    (trashed["id"],),
                )
            report.restored.append(trashed["id"])
            log_event(logger, "import.restore", asset_id=trashed["id"], sha256=sha[:16])
            return

        # 3) 全新导入
        width, height, fmt = validate_image(path)
        when = datetime.now(timezone.utc)
        fname = unique_filename(
            self.config.images_dir(),
            self.config.naming_template,
            when=when,
            source=source,
            ext=path.suffix.lstrip(".") or "png",
        )
        target = self.config.images_dir() / fname
        data = path.read_bytes()
        atomic_write(target, data)  # 原图只读落盘（原子）

        with self.db.tx() as conn:
            cur = conn.execute(
                "INSERT INTO assets(path, original_path, sha256, width, height, "
                "format, size_bytes, source, created_at, imported_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) RETURNING id",
                (
                    str(target), str(path), sha, width, height, fmt,
                    len(data), source,
                    when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                    when.strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
                ),
            )
            asset_id = cur.fetchone()[0]
            conn.execute(
                "INSERT INTO audit_log(action, detail, asset_id) "
                "VALUES ('import', ?, ?)",
                (f"source={source} sha256={sha[:16]} file={path.name}", asset_id),
            )
        self.jobs.enqueue(asset_id, "ocr", priority=0)
        self.jobs.enqueue(asset_id, "thumb", priority=-1)
        report.imported.append(asset_id)
        log_event(logger, "import.ok", asset_id=asset_id, sha256=sha[:16], size=len(data))
