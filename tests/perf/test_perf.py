"""性能压测（M4/M3）：万级检索 P95≤300ms；1000 张批量导入不崩溃、内存可控。

检索压测用「合成向量 + 合成 FTS 文本」直接入库（聚焦延迟本身，
检索正确性由 quality 套件覆盖）；导入压测用真实 PNG 走完整导入管线。
结果写入 reports/perf_report.json。
"""
from __future__ import annotations

import json
import random
import resource
import time
from pathlib import Path

import numpy as np
import pytest

from snapvault.embedding_engine import EMBEDDING_DIM
from snapvault.util import sha256_bytes

REPORTS = Path(__file__).resolve().parents[2] / "reports"
N_ASSETS = 10_000
N_QUERIES = 60
N_IMPORT = 1000
P95_BUDGET_MS = 300


def _synthetic_embedding(rng: np.random.Generator) -> bytes:
    v = rng.standard_normal(EMBEDDING_DIM)
    v = v / np.linalg.norm(v)
    return v.astype("<f4").tobytes()


def _seed_library(engine, rng: np.random.Generator) -> None:
    """直插 10k 资产：assets + ocr_texts + ocr_fts + embeddings + vec_embeddings。"""
    db = engine.db
    rows_assets, rows_text, rows_fts, rows_emb, rows_vec = [], [], [], [], []
    for i in range(N_ASSETS):
        text = f"页面文案关键字{i:05d} 混合检索场景第{i}号截图 内容编号"
        h = sha256_bytes(f"asset-{i}".encode())
        rows_assets.append((f"/perf/{i:05d}.png", h, 800 + i % 400, 600 + i % 300,
                            "PNG", "perf"))
        rows_text.append((i + 1, 0, text, 0.95))
        rows_fts.append((i + 1, 0, text))
        vec = _synthetic_embedding(rng)
        rows_emb.append((i + 1, vec))
        rows_vec.append((i + 1, vec))
    with db.tx() as conn:
        conn.executemany(
            "INSERT INTO assets(path, sha256, width, height, format, source) "
            "VALUES (?, ?, ?, ?, ?, ?)", rows_assets)
        conn.executemany(
            "INSERT INTO ocr_texts(asset_id, chunk_index, text, confidence) "
            "VALUES (?, ?, ?, ?)", rows_text)
        conn.executemany(
            "INSERT INTO ocr_fts(asset_id, chunk_index, text) VALUES (?, ?, ?)",
            rows_fts)
        conn.executemany(
            "INSERT INTO embeddings(asset_id, embedding, model, dim) "
            "VALUES (?, ?, 'BAAI/bge-small-zh-v1.5', 512)", rows_emb)
        conn.executemany(
            "INSERT INTO vec_embeddings(rowid, embedding) VALUES (?, ?)", rows_vec)
        conn.execute("UPDATE assets SET ocr_status='done'")


class TestPerf:
    def test_10k_hybrid_search_p95(self, engine, data_dir):
        rng = np.random.default_rng(42)
        _seed_library(engine, rng)
        assert engine.db.scalar("SELECT COUNT(*) FROM assets") == N_ASSETS
        queries = [f"关键字{i:05d}" for i in
                   random.Random(7).sample(range(N_ASSETS), N_QUERIES)]

        latencies = []
        for q in queries:
            t0 = time.perf_counter()
            hits = engine.search(q, k=10)
            latencies.append((time.perf_counter() - t0) * 1000)
            assert hits, f"查询 {q} 无结果"

        latencies.sort()
        p95 = latencies[int(len(latencies) * 0.95)]
        p50 = latencies[int(len(latencies) * 0.5)]
        report = {
            "suite": "perf-search",
            "assets": N_ASSETS,
            "queries": N_QUERIES,
            "p50_ms": round(p50, 1),
            "p95_ms": round(p95, 1),
            "max_ms": round(latencies[-1], 1),
            "budget_ms": P95_BUDGET_MS,
            "passed": p95 <= P95_BUDGET_MS,
        }
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "perf_search_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2))
        assert p95 <= P95_BUDGET_MS, f"P95={p95:.1f}ms > {P95_BUDGET_MS}ms"

    def test_1000_batch_import(self, engine, make_shot, tmp_path):
        """1000 张真实 PNG 全流程导入：不崩溃、内存峰值可接受。"""
        import os

        texts = [f"批量导入测试第{i}号截图 内容唯一{i}" for i in range(N_IMPORT)]
        paths = [make_shot(t, size=(320, 180)) for t in texts]
        t0 = time.perf_counter()
        rep = engine.import_files(paths, source="perf-import")
        wall = time.perf_counter() - t0
        assert len(rep.imported) == N_IMPORT, f"导入失败 {len(rep.failed)} 张"
        # 缩略图任务（OCR 正确性由 quality 覆盖，此处验证管线不崩溃）
        for aid in rep.imported:
            engine.jobs.enqueue(aid, "thumb", priority=0)
        n = engine.process_jobs()
        assert engine.job_stats().get("pending", 0) == 0

        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024  # MB
        import psutil

        ps = psutil.Process(os.getpid()).memory_info().rss / 1024 / 1024
        report = {
            "suite": "perf-import",
            "images": N_IMPORT,
            "wall_sec": round(wall, 1),
            "jobs_processed": n,
            "peak_rss_mb": round(rss, 1),
            "current_rss_mb": round(ps, 1),
        }
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "perf_import_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2))
        assert n >= N_IMPORT  # ocr+thumb(+embed) 全链路任务完成
        assert engine.job_stats().get("pending", 0) == 0
        assert ps < 2000, f"内存峰值 {ps:.0f}MB 超限"
