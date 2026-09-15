"""SnapVault 核心逻辑外观层（engine）：UI 与 CLI 共用的唯一入口。

engine.SnapVault 装配：Config + Database + 各领域服务 + 惰性模型引擎，
并提供 process_jobs（同步处理异步队列，供 CLI/测试/无 UI 场景使用）。
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable

from .annotations import AnnotationModel, AnnotationStore, Element
from .backup import BackupManager
from .config import Config
from .db import Database
from .embedding_engine import EmbeddingEngine
from .exporter import ExportOptions, ExportReport, Exporter
from .importer import ImportReport, Importer
from .jobs import JobManager
from .logging_setup import log_event, setup_logging
from .metadata import MetadataService
from .ocr_engine import OcrEngine
from .pipeline import embed_handler, ocr_handler, thumb_handler
from .privacy import NetworkProbe, PrivacyReport
from .rebuild import RebuildManager, RebuildReport
from .search import HybridSearch, SearchFilter, SearchHit
from .single_instance import SingleInstanceLock

logger = logging.getLogger("snapvault.engine")

__all__ = [
    "SnapVault", "Config", "Database",
    "ImportReport", "ExportOptions", "ExportReport", "SearchFilter", "SearchHit",
    "AnnotationModel", "Element", "PrivacyReport", "RebuildReport",
]


class SnapVault:
    """端到端核心入口：数据层 → OCR/向量 → 检索 → 元数据/标注 → 导出/备份。"""

    def __init__(self, data_dir: str | Path | None = None,
                 acquire_lock: bool = True, setup_logs: bool = True):
        self.config = Config(data_dir)
        if setup_logs:
            setup_logging(self.config.log_path())
        if acquire_lock:
            self._lock = SingleInstanceLock(self.config.root)
            self._lock.acquire()
        self.db = Database(self.config.db_path())
        self.jobs = JobManager(self.db)
        self.metadata = MetadataService(self.db, self.config)
        self.annotations = AnnotationStore(self.db, self.config)
        self.exporter = Exporter(self.db, self.config)
        self.backup = BackupManager(self.db, self.config)
        self.rebuild = RebuildManager(self.db, self.config)
        self._ocr: OcrEngine | None = None
        self._embedder: EmbeddingEngine | None = None
        self._search: HybridSearch | None = None
        self._privacy: NetworkProbe = NetworkProbe()
        log_event(logger, "engine.ready", data_dir=str(self.config.root),
                  schema=self.db.scalar("PRAGMA user_version"))

    # ------------------------------------------------------------------
    # 惰性引擎
    @property
    def ocr(self) -> OcrEngine:
        if self._ocr is None:
            # 模型随 snapvault-core 包分发（core/snapvault/models/rapidocr）
            self._ocr = OcrEngine()
        return self._ocr

    @property
    def embedder(self) -> EmbeddingEngine:
        if self._embedder is None:
            self._embedder = EmbeddingEngine(self.config.models_dir() / "fastembed")
        return self._embedder

    @property
    def search_engine(self) -> HybridSearch:
        if self._search is None:
            self._search = HybridSearch(self.db, self.embedder,
                                        notes_in_fts=self.config.notes_in_fts)
        return self._search

    # ------------------------------------------------------------------
    # 导入
    def import_files(self, paths, source: str = "import",
                     on_progress=None) -> ImportReport:
        report = Importer(self.db, self.config).import_files(
            paths, source=source, on_progress=on_progress)
        self._enqueue_pipeline(report)
        return report

    def import_directory(self, dir_path, recursive: bool = True,
                         source: str = "import", on_progress=None) -> ImportReport:
        report = Importer(self.db, self.config).import_directory(
            dir_path, recursive=recursive, source=source, on_progress=on_progress)
        self._enqueue_pipeline(report)
        return report

    def _enqueue_pipeline(self, report: ImportReport) -> None:
        for aid in report.imported + report.restored:
            self.jobs.enqueue(aid, "ocr", priority=10)
            self.jobs.enqueue(aid, "thumb", priority=0)

    # ------------------------------------------------------------------
    # 异步任务（同步跑完；崩溃恢复时先 reset 再跑）
    def process_jobs(self, max_runs: int | None = None, sleep_sec: float = 0.05) -> int:
        handlers = {
            "ocr": ocr_handler(self.db, self.config, self.ocr),
            "embed": embed_handler(self.db, self.config, self.embedder),
            "thumb": thumb_handler(self.db, self.config),
        }
        return self.jobs.process_loop(handlers, max_runs=max_runs, sleep_sec=sleep_sec)

    def reset_running_jobs(self) -> int:
        return self.jobs.reset_running()

    def job_stats(self) -> dict:
        return self.jobs.stats()

    # ------------------------------------------------------------------
    # 检索
    def search(self, query: str, k: int = 20,
               filters: SearchFilter | None = None) -> list[SearchHit]:
        return self.search_engine.search(query, k=k, filters=filters)

    def similar(self, asset_id: int, k: int = 10) -> list[SearchHit]:
        return self.search_engine.similar(asset_id, k=k)

    # ------------------------------------------------------------------
    # 导出 / 备份 / 重建
    def export(self, options: ExportOptions | None = None) -> ExportReport:
        return self.exporter.export(options or ExportOptions())

    def export_library(self, dst=None, on_progress=None) -> str:
        return self.backup.export_library(dst=dst, on_progress=on_progress)

    def import_library(self, zip_path, target_root=None):
        return self.backup.import_library(zip_path, target_root=target_root)

    def rebuild_indexes(self, ocr=True, embed=True, thumbs=True,
                        force=False) -> RebuildReport:
        return self.rebuild.rebuild_all(ocr=ocr, embed=embed, thumbs=thumbs, force=force)

    # ------------------------------------------------------------------
    # 隐私自检 / 统计
    def privacy_check(self) -> PrivacyReport:
        return self._privacy.scan()

    def stats(self) -> dict:
        return {
            "assets": int(self.db.scalar("SELECT COUNT(*) FROM assets") or 0),
            "trashed": int(self.db.scalar(
                "SELECT COUNT(*) FROM assets WHERE deleted_at IS NOT NULL") or 0),
            "tags": int(self.db.scalar("SELECT COUNT(*) FROM tags") or 0),
            "notes": int(self.db.scalar("SELECT COUNT(*) FROM notes") or 0),
            "jobs": self.jobs.stats(),
            "db_size": self.config.db_path().stat().st_size
            if self.config.db_path().exists() else 0,
        }

    def close(self) -> None:
        self.db.close()
        lock = getattr(self, "_lock", None)
        if lock is not None:
            lock.release()
