"""稳定性测试（M2）：进程在 OCR 中途被 kill -9，重启后数据一致、任务自动恢复。

方案：
1. worker 子进程：导入 20 张图并跑 OCR 任务；ocr_handler 在「OCR 完成但尚未提交」之间
   睡眠（SNAPVAULT_TEST_SLEEP_MS），父进程在该窗口 kill -9。
2. 父进程校验：DB 完整性 ok；无孤儿文件（images 目录中每个文件都被 assets 引用）；
   reset_running 后重新处理队列，全部任务 done；ocr_texts 与 assets 一一对应。

结果写入 reports/stability_report.json。
"""
from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

from snapvault.db import Database

REPORTS = Path(__file__).resolve().parents[2] / "reports"
CORE = Path(__file__).resolve().parents[2] / "core"
WORKER = Path(__file__).resolve().parent / "worker_ocr.py"
N_IMAGES = 20


def _run_worker(data_dir: Path, images: list[Path], sleep_ms: int) -> subprocess.Popen:
    env = {**os.environ, "PYTHONPATH": str(CORE),
           "SNAPVAULT_DATA_DIR": str(data_dir),
           "SNAPVAULT_TEST_SLEEP_MS": str(sleep_ms)}
    return subprocess.Popen(
        [sys.executable, str(WORKER), *[str(p) for p in images]],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )


class TestStability:
    def test_kill_during_ocr_then_recover(self, data_dir, make_shot):
        images = [make_shot(f"稳定性测试图片{i} 内容编号{i:02d}") for i in range(N_IMAGES)]
        proc = _run_worker(data_dir, images, sleep_ms=1200)

        # 等待 worker 完成导入并进入 OCR 提交前睡眠窗口
        deadline = time.time() + 120
        sentinel = data_dir / ".worker_ready"
        while time.time() < deadline:
            if sentinel.exists():
                break
            time.sleep(0.2)
        assert sentinel.exists(), "worker 未进入 OCR 窗口"

        # 等首个 OCR 进入睡眠后 kill -9
        time.sleep(0.6)
        proc.send_signal(signal.SIGKILL)
        proc.wait()
        killed_after_import = True

        # ===== 校验 1：数据库完整性 =====
        db_path = data_dir / "db" / "snapvault.db"
        db = Database(db_path)
        assert db.integrity_ok(), "kill 后数据库完整性损坏"
        n_assets = int(db.scalar("SELECT COUNT(*) FROM assets") or 0)
        assert n_assets == N_IMAGES

        # ===== 校验 2：无孤儿文件 =====
        referenced = {Path(r["path"]).resolve() for r in
                      db.query("SELECT path FROM assets")}
        on_disk = {p.resolve() for p in (data_dir / "images").glob("*") if p.is_file()}
        orphans = on_disk - referenced
        assert not orphans, f"存在孤儿文件: {orphans}"

        # ===== 校验 3：无半写入记录 =====
        # 被 kill 的 ocr 任务可能残留 running 状态或 ocr_texts 不完整
        running = int(db.scalar("SELECT COUNT(*) FROM jobs WHERE status='running'") or 0)
        inconsistent = int(db.scalar(
            "SELECT COUNT(*) FROM assets a LEFT JOIN ocr_texts o ON o.asset_id=a.id "
            "WHERE a.ocr_status='done' AND o.asset_id IS NULL") or 0)
        assert inconsistent == 0, f"{inconsistent} 个资产标记 done 但无 OCR 文本"

        # ===== 校验 4：重启后任务自动恢复 =====
        from snapvault.engine import SnapVault

        engine = SnapVault(data_dir, acquire_lock=False, setup_logs=False)
        try:
            reset = engine.reset_running_jobs()
            assert reset >= 0
            n = engine.process_jobs()
            stats = engine.job_stats()
            assert stats.get("pending", 0) == 0, f"仍有任务未处理: {stats}"
            # 全部资产 OCR 完成且文本与资产一一对应
            done = int(engine.db.scalar(
                "SELECT COUNT(*) FROM assets WHERE ocr_status='done'") or 0)
            assert done == N_IMAGES
            # 半写入校验：无 done 且无 ocr 文本的资产
            assert int(engine.db.scalar(
                "SELECT COUNT(*) FROM assets a WHERE a.ocr_status='done' "
                "AND NOT EXISTS (SELECT 1 FROM ocr_texts o WHERE o.asset_id=a.id)") or 0) == 0
        finally:
            engine.close()

        # ===== 校验 5：检索仍可用（数据一致） =====
        engine = SnapVault(data_dir, acquire_lock=False, setup_logs=False)
        try:
            hits = engine.search("稳定性测试图片0", k=3)
            assert hits and hits[0].asset_id == 1
        finally:
            engine.close()

        report = {
            "suite": "stability",
            "images": N_IMAGES,
            "kill_timing": "during-ocr-before-commit",
            "integrity_ok": True,
            "orphans": 0,
            "half_written": 0,
            "jobs_recovered": True,
            "passed": True,
        }
        REPORTS.mkdir(exist_ok=True)
        (REPORTS / "stability_report.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2))
