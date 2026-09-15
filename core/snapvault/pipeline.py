"""异步任务处理器：OCR → 写 FTS + 入队 embedding；embedding → 写向量库；缩略图。

OCR 结果写入同时覆盖 FTS5 全文索引；embedding 写入 sqlite-vec 向量索引。
处理器全部幂等：重复执行先清旧数据再写；同哈希只 OCR 一次由 jobs 去重保证。
"""
from __future__ import annotations

import json
import logging
from io import BytesIO

from .config import Config
from .db import Database
from .embedding_engine import EMBEDDING_DIM, EmbeddingEngine
from .jobs import JobManager
from .ocr_engine import OcrEngine
from .util import atomic_write

logger = logging.getLogger("snapvault.pipeline")

MAX_EMBED_CHARS = 2000


def ocr_handler(db: Database, config: Config, ocr: OcrEngine) -> callable:
    jobs = JobManager(db)

    def _handle(job: dict) -> None:
        asset_id = job["asset_id"]
        asset = db.query_one("SELECT path FROM assets WHERE id=?", (asset_id,))
        if asset is None:
            return
        result = ocr.recognize(asset["path"])
        with db.tx() as conn:
            # 幂等：清旧再写
            conn.execute("DELETE FROM ocr_fts WHERE asset_id=?", (asset_id,))
            conn.execute("DELETE FROM ocr_texts WHERE asset_id=?", (asset_id,))
            for i, chunk in enumerate(result.chunks):
                conn.execute(
                    "INSERT INTO ocr_texts(asset_id, chunk_index, text, "
                    "confidence, bbox, lang) VALUES (?, ?, ?, ?, ?, ?)",
                    (asset_id, i, chunk.text, chunk.confidence,
                     json.dumps([round(v, 1) for v in chunk.bbox], separators=(",", ":")),
                     chunk.lang),
                )
                conn.execute(
                    "INSERT INTO ocr_fts(asset_id, chunk_index, text) VALUES (?, ?, ?)",
                    (asset_id, i, chunk.text),
                )
            conn.execute(
                "UPDATE assets SET ocr_status='done', ocr_confidence=? WHERE id=?",
                (result.mean_confidence(), asset_id),
            )
        if result.chunks:
            jobs.enqueue(asset_id, "embed", priority=1)
        logger.info("ocr.done asset_id=%s chunks=%s elapsed_ms=%.0f",
                    asset_id, len(result.chunks), result.elapsed_ms)

    return _handle


def embed_handler(db: Database, config: Config, embedder: EmbeddingEngine) -> callable:
    def _handle(job: dict) -> None:
        asset_id = job["asset_id"]
        texts = db.query(
            "SELECT text FROM ocr_texts WHERE asset_id=? ORDER BY chunk_index",
            (asset_id,),
        )
        parts = [r["text"] for r in texts]
        if config.notes_in_fts:
            note = db.scalar("SELECT content FROM notes WHERE asset_id=?", (asset_id,))
            if note:
                parts.append(note)
        joined = "\n".join(parts)[:MAX_EMBED_CHARS].strip()
        if not joined:
            return
        vec = embedder.embed_one(joined)
        blob = vec.astype("<f4").tobytes()
        with db.tx() as conn:
            conn.execute("DELETE FROM vec_embeddings WHERE rowid=?", (asset_id,))
            conn.execute(
                "INSERT INTO vec_embeddings(rowid, embedding) VALUES (?, ?)",
                (asset_id, blob),
            )
            conn.execute(
                "INSERT INTO embeddings(asset_id, embedding, model, dim) "
                "VALUES (?, ?, ?, ?) "
                "ON CONFLICT(asset_id) DO UPDATE SET "
                "embedding=excluded.embedding, model=excluded.model, dim=excluded.dim",
                (asset_id, blob, embedder.model_name, EMBEDDING_DIM),
            )
        logger.info("embed.done asset_id=%s dim=%s", asset_id, EMBEDDING_DIM)

    return _handle


def thumb_handler(db: Database, config: Config, size: int = 256) -> callable:
    def _handle(job: dict) -> None:
        asset_id = job["asset_id"]
        asset = db.query_one("SELECT path FROM assets WHERE id=?", (asset_id,))
        if asset is None:
            return
        from PIL import Image

        with Image.open(asset["path"]) as img:
            img = img.convert("RGB")
            img.thumbnail((size, size))
            bio = BytesIO()
            img.save(bio, "JPEG", quality=82)
        out = config.thumbnails_dir() / f"{asset_id}.jpg"
        atomic_write(out, bio.getvalue())  # 原子写

    return _handle
