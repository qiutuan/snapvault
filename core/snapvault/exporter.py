"""导出（M7）：按筛选条件批量导出 图片目录 + CSV 汇总 + PDF 图文报告。

- 图片可用"标注后版本"或"原图"（原图只读，标注版本内存栅格化后另存）
- 导出包名含时间戳；过程可取消（cancel Event）
- 写入先落临时目录，全部成功后再 rename 为最终包名，避免半成品
"""
from __future__ import annotations

import csv
import json
import logging
import shutil
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .annotations import AnnotationModel, AnnotationStore, rasterize
from .config import Config
from .db import Database
from .exceptions import ExportError
from .importer import validate_image
from .logging_setup import log_event
from .search import SearchFilter, build_asset_filter
from .util import atomic_write, human_size

logger = logging.getLogger("snapvault.exporter")

FONT_PATH = Path(__file__).resolve().parents[2] / "fonts" / "NotoSansSC-Regular.otf"
PDF_MAX_ASSETS = 2000  # PDF 报告页数防御上限


class ExportCanceled(Exception):
    """用户取消导出。"""


@dataclass
class ExportOptions:
    include: str = "annotated"          # annotated | original
    with_csv: bool = True
    with_pdf: bool = True
    filters: SearchFilter | None = None
    pdf_max_assets: int = PDF_MAX_ASSETS


@dataclass
class ExportReport:
    package_dir: str = ""
    asset_count: int = 0
    csv_path: str = ""
    pdf_path: str = ""
    canceled: bool = False
    errors: list[str] = field(default_factory=list)


class Exporter:
    def __init__(self, db: Database, config: Config):
        self.db = db
        self.config = config
        self.annotations = AnnotationStore(db, config)

    # ------------------------------------------------------------------
    def export(
        self,
        options: ExportOptions,
        on_progress: Callable[[int, int], None] | None = None,
        cancel: threading.Event | None = None,
    ) -> ExportReport:
        report = ExportReport()
        if options.include not in ("annotated", "original"):
            raise ValueError(f"include 参数非法: {options.include}")

        clause, params = build_asset_filter(options.filters or SearchFilter())
        rows = self.db.query(
            f"SELECT * FROM assets AS a WHERE {clause} ORDER BY a.id", tuple(params)
        )
        if not rows:
            raise ExportError("没有符合筛选条件的截图可导出")

        stamp = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S_%f")
        # 唯一包名：微秒时间戳 + 随机后缀，避免同秒并发导出互相覆盖
        import uuid

        pkg_base = self.config.exports_dir() / f"snapvault_export_{stamp}_{uuid.uuid4().hex[:6]}"
        tmp_pkg = self.config.exports_dir() / f".export_{stamp}_{uuid.uuid4().hex[:6]}_tmp"
        final_pkg = pkg_base
        images_out = tmp_pkg / "images"
        images_out.mkdir(parents=True, exist_ok=True)

        field_defs = self.db.query(
            "SELECT key, label FROM custom_field_defs ORDER BY id")
        field_keys = [r["key"] for r in field_defs]
        field_labels = {r["key"]: r["label"] for r in field_defs}

        total = len(rows)
        csv_rows: list[dict] = []
        pdf_pages: list[dict] = []
        errors: list[str] = []
        try:
            for idx, asset in enumerate(rows, start=1):
                if cancel is not None and cancel.is_set():
                    report.canceled = True
                    break
                try:
                    fname = self._write_image(asset, images_out, options.include)
                    if fname is None:
                        continue
                    csv_rows.append(self._csv_row(asset, fname, field_keys))
                    if options.with_pdf and idx <= options.pdf_max_assets:
                        pdf_pages.append(self._pdf_row(asset, fname))
                except Exception as exc:  # noqa: BLE001
                    errors.append(f"#{asset['id']} {Path(asset['path']).name}: {exc}")
                if on_progress:
                    on_progress(idx, total)
        except Exception:
            shutil.rmtree(tmp_pkg, ignore_errors=True)
            raise

        report.asset_count = len(csv_rows)
        report.errors = errors
        if report.canceled:
            shutil.rmtree(tmp_pkg, ignore_errors=True)  # 取消：清理临时目录
            return report
        if errors:
            log_event(logger, "export.partial_errors", count=len(errors))

        # 写 CSV / PDF / manifest 到临时包
        if options.with_csv and csv_rows:
            report.csv_path = self._write_csv(tmp_pkg, csv_rows, field_keys, field_labels)
        if options.with_pdf and pdf_pages:
            report.pdf_path = self._write_pdf(tmp_pkg, pdf_pages, field_keys, field_labels)
        manifest = {
            "app": "snapvault",
            "exported_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ"),
            "include": options.include,
            "asset_count": report.asset_count,
            "errors": errors,
        }
        atomic_write(
            tmp_pkg / "manifest.json",
            json.dumps(manifest, ensure_ascii=False, indent=2).encode("utf-8"),
        )

        # 临时目录 → 最终包名（原子 rename）
        shutil.move(str(tmp_pkg), str(final_pkg))
        report.package_dir = str(final_pkg)
        if report.csv_path:
            report.csv_path = str(final_pkg / "metadata.csv")
        if report.pdf_path:
            report.pdf_path = str(final_pkg / "report.pdf")
        size = sum(p.stat().st_size for p in final_pkg.rglob("*") if p.is_file())
        log_event(logger, "export.done", package=str(final_pkg), count=report.asset_count,
                  size=human_size(size))
        return report

    # ------------------------------------------------------------------
    def _write_image(self, asset, images_out: Path, include: str) -> str | None:
        src = Path(asset["path"])
        if not src.exists():
            raise FileNotFoundError(f"原图缺失: {src}")
        fname = f"{asset['id']:06d}_{src.name}"
        target = images_out / fname
        # 可读性校验：损坏图片在此被隔离（单条错误，不影响整包导出）
        try:
            validate_image(src)
        except ValueError as exc:
            raise ValueError(f"图片已损坏，已隔离: {exc}") from exc
        if include == "original":
            shutil.copyfile(src, target)
            return fname
        # annotated：有标注则栅格化，无标注回退原图
        model = self.annotations.get_model(asset["id"])
        if model.elements:
            img = rasterize(src, model, font_path=str(FONT_PATH) if FONT_PATH.exists() else None)
            from io import BytesIO

            bio = BytesIO()
            img.save(bio, "PNG")
            atomic_write(target, bio.getvalue())
        else:
            shutil.copyfile(src, target)
        return fname

    def _csv_row(self, asset, fname: str, field_keys: list[str]) -> dict:
        row = {
            "asset_id": asset["id"],
            "file": fname,
            "created_at": asset["created_at"],
            "width": asset["width"],
            "height": asset["height"],
            "format": asset["format"],
            "source": asset["source"],
            "sha256": asset["sha256"],
            "ocr_confidence": "" if asset["ocr_confidence"] is None
            else round(asset["ocr_confidence"], 4),
        }
        row["tags"] = "|".join(
            r["name"] for r in self.db.query(
                "SELECT t.name FROM asset_tags at JOIN tags t ON t.id=at.tag_id "
                "WHERE at.asset_id=? ORDER BY t.name", (asset["id"],)))
        row["note"] = (self.db.scalar("SELECT content FROM notes WHERE asset_id=?",
                                      (asset["id"],)) or "")
        texts = [r["text"] for r in self.db.query(
            "SELECT text FROM ocr_texts WHERE asset_id=? ORDER BY chunk_index",
            (asset["id"],))]
        row["ocr_text"] = "\n".join(texts)
        values = {r["key"]: r["value"] for r in self.db.query(
            "SELECT cfd.key, cfv.value FROM custom_field_values cfv "
            "JOIN custom_field_defs cfd ON cfd.id=cfv.field_id "
            "WHERE cfv.asset_id=?", (asset["id"],))}
        for k in field_keys:
            row[k] = values.get(k, "")
        return row

    def _pdf_row(self, asset, fname: str) -> dict:
        texts = [r["text"] for r in self.db.query(
            "SELECT text FROM ocr_texts WHERE asset_id=? ORDER BY chunk_index",
            (asset["id"],))]
        return {
            "asset_id": asset["id"], "fname": fname,
            "created_at": asset["created_at"],
            "note": (self.db.scalar("SELECT content FROM notes WHERE asset_id=?",
                                    (asset["id"],)) or ""),
            "tags": "|".join(r["name"] for r in self.db.query(
                "SELECT t.name FROM asset_tags at JOIN tags t ON t.id=at.tag_id "
                "WHERE at.asset_id=? ORDER BY t.name", (asset["id"],))),
            "ocr": "\n".join(texts)[:1500],
        }

    # ------------------------------------------------------------------
    def _write_csv(self, pkg: Path, rows: list[dict], field_keys: list[str],
                   field_labels: dict) -> str:
        base_cols = ["asset_id", "file", "created_at", "width", "height", "format",
                     "source", "sha256", "ocr_confidence", "tags", "note"]
        cols = base_cols + [field_labels.get(k, k) for k in field_keys] + ["ocr_text"]
        # 行内使用字段 key，写出时映射为展示 label 列
        out_rows = []
        for row in rows:
            mapped = dict(row)
            for k in field_keys:
                mapped[field_labels.get(k, k)] = mapped.pop(k, "")
            out_rows.append(mapped)
        path = pkg / "metadata.csv"
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=cols, extrasaction="ignore")
            writer.writeheader()
            writer.writerows(out_rows)
        return str(path)

    def _write_pdf(self, pkg: Path, pages: list[dict], field_keys: list[str],
                   field_labels: dict) -> str:
        from reportlab.lib import colors
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import (
            Image as RLImage, PageBreak, Paragraph, SimpleDocTemplate, Spacer,
        )

        if FONT_PATH.exists():
            try:
                pdfmetrics.registerFont(TTFont("NotoSC", str(FONT_PATH)))
                _FONT = "NotoSC"
            except Exception:  # noqa: BLE001
                _FONT = "Helvetica"
        else:
            _FONT = "Helvetica"

        style = ParagraphStyle("body", fontName=_FONT, fontSize=9, leading=13,
                               wordWrap="CJK")
        title_style = ParagraphStyle("title", fontName=_FONT, fontSize=12,
                                     leading=16, spaceAfter=6)
        path = pkg / "report.pdf"
        doc = SimpleDocTemplate(str(path), pagesize=A4,
                                leftMargin=15 * mm, rightMargin=15 * mm,
                                topMargin=12 * mm, bottomMargin=12 * mm,
                                title="SnapVault 导出报告")
        story = []
        for i, page in enumerate(pages, start=1):
            img_file = pkg / "images" / page["fname"]
            story.append(Paragraph(
                f"#{page['asset_id']} · {page['fname']} · {page['created_at']}",
                title_style))
            try:
                from PIL import Image as PILImage

                with PILImage.open(img_file) as im:
                    w, h = im.size
                scale = min(1.0, 160 * mm / w, 100 * mm / h)
                story.append(RLImage(str(img_file), width=w * scale, height=h * scale))
            except Exception:  # noqa: BLE001
                story.append(Paragraph("[图片不可用]", style))
            if page["tags"]:
                story.append(Paragraph(f"标签：{page['tags']}", style))
            if page["note"]:
                story.append(Paragraph(f"备注：{page['note']}", style))
            if page["ocr"]:
                story.append(Paragraph(f"OCR：{page['ocr']}", style))
            story.append(Spacer(1, 4 * mm))
            story.append(PageBreak())
        doc.build(story)
        return str(path)
