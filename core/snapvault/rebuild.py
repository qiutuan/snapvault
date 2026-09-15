"""索引重建（M1/M4）：从 数据库+图片文件 离线重建 FTS 全文索引与 sqlite-vec 向量索引。

rebuild_all(force=False) 对每张资产：
1. OCR（若 ocr_status != done 或 force）→ 写 ocr_texts + ocr_fts
2. 有 OCR 文本则入队/直写 embedding → vec_embeddings
3. 缩略图缺失或 force → 重建缩略图
全部幂等：先清旧索引再写。
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from .config import Config
from .db import Database
from .embedding_engine import EmbeddingEngine
from .jobs import JobManager
from .ocr_engine import OcrEngine
from .pipeline import embed_handler, ocr_handler, thumb_handler

logger = logging.getLogger("snapvault.rebuild")


@dataclass
class RebuildReport:
    total: int = 0
    ocr_done: int = 0
    ocr_skipped: int = 0
    embed_done: int = 0
    thumbs_done: int = 0
    failed: list[tuple[int, str]] = None  # type: ignore[assignment]

    def __post_init__(self):
        self.failed = self.failed or []


class RebuildManager:
    def __init__(self, db: Database, config: Config, ocr: OcrEngine | None = None,
                 embedder: EmbeddingEngine | None = None):
        self.db = db
        self.config = config
        self.ocr = ocr
        self.embedder = embedder

    # ------------------------------------------------------------------
    def rebuild_all(self, ocr: bool = True, embed: bool = True,
                    thumbs: bool = True, force: bool = False) -> RebuildReport:
        report = RebuildReport()
        assets = self.db.query(
            "SELECT id, path FROM assets ORDER BY id"
        )
        report.total = len(assets)
        jobs = JobManager(self.db)

        for row in assets:
            aid, path = row["id"], row["path"]
            try:
                if ocr:
                    st = self.db.scalar(
                        "SELECT ocr_status FROM assets WHERE id=?", (aid,))
                    if force or st != "done":
                        if self.ocr is None:
                            self.ocr = OcrEngine()  # 模型随包分发
                        ocr_handler(self.db, self.config, self.ocr)({
                            "asset_id": aid, "job_id": 0})
                        report.ocr_done += 1
                    else:
                        report.ocr_skipped += 1
                if embed:
                    has_vec = self.db.scalar(
                        "SELECT COUNT(*) FROM embeddings WHERE asset_id=? AND dim>0",
                        (aid,))
                    if force or not has_vec:
                        if self.embedder is None:
                            self.embedder = EmbeddingEngine(
                                self.config.models_dir() / "fastembed")
                        embed_handler(self.db, self.config, self.embedder)({
                            "asset_id": aid, "job_id": 0})
                        report.embed_done += 1
                if thumbs:
                    tpath = self.config.thumbnails_dir() / f"{aid:06d}.jpg"
                    if force or not tpath.exists():
                        thumb_handler(self.db, self.config)({"asset_id": aid, "job_id": 0})
                        report.thumbs_done += 1
            except Exception as exc:  # noqa: BLE001
                report.failed.append((aid, str(exc)))
                logger.exception("rebuild failed asset=%s", aid)
        logger.info("rebuild.done total=%s ocr=%s embed=%s thumbs=%s failed=%s",
                    report.total, report.ocr_done, report.embed_done,
                    report.thumbs_done, len(report.failed))
        return report

    # ------------------------------------------------------------------
    def reset_jobs(self) -> int:
        """将 pending/running 队列重置（崩溃恢复入口），返回受影响数。"""
        return JobManager(self.db).reset_running()
