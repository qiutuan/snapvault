"""异步任务队列：OCR / embedding / 缩略图等。

- 幂等：同 (asset_id, type) 未完成时不重复入队
- 失败重试：3 次指数退避（2s, 4s, 8s）
- 崩溃恢复：启动时 reset_running() 将 running 恢复为 pending，断点续扫
- 单条 claim 用 UPDATE...RETURNING 原子完成，杜绝并发重复领取
"""
from __future__ import annotations

import random
import time
from datetime import datetime, timezone
from typing import Iterable

from .db import Database
from .timeutil import utc_from_epoch, utc_now

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_DONE = "done"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


def _now() -> str:
    return utc_now()


def _iso_to_ts(iso: str | None) -> float:
    if not iso:
        return 0.0
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%fZ"):  # 新格式 + 旧版兼容
        try:
            dt = datetime.strptime(iso, fmt)
            return dt.replace(tzinfo=timezone.utc).timestamp()
        except ValueError:
            continue
    return 0.0


class JobManager:
    def __init__(self, db: Database):
        self.db = db

    # ------------------------------------------------------------------
    def enqueue(
        self,
        asset_id: int,
        job_type: str,
        priority: int = 0,
        max_attempts: int = 3,
    ) -> int | None:
        """入队（幂等：同 asset_id+type 存在 pending/running 时跳过）。"""
        exists = self.db.scalar(
            "SELECT COUNT(*) FROM jobs WHERE asset_id=? AND type=? "
            "AND status IN ('pending','running')",
            (asset_id, job_type),
        )
        if exists:
            return None
        cur = self.db.execute(
            "INSERT INTO jobs(asset_id, type, status, priority, max_attempts) "
            "VALUES (?, ?, 'pending', ?, ?) RETURNING id",
            (asset_id, job_type, priority, max_attempts),
        )
        return cur.fetchone()[0]

    # ------------------------------------------------------------------
    def claim(self, job_types: Iterable[str] | None = None, now: float | None = None) -> dict | None:
        """原子领取一个到期任务并标记 running，返回任务 dict。"""
        now = now if now is not None else time.time()
        now_iso = _now()
        types_sql = ""
        params: list = [now_iso, now_iso]  # started_at, next_retry_at
        if job_types:
            placeholders = ",".join("?" for _ in job_types)
            types_sql = f" AND type IN ({placeholders})"
            params.extend(job_types)
        row = self.db.execute(
            "UPDATE jobs SET status='running', started_at=?, "
            "attempts = attempts + 1 "
            "WHERE id = ("
            "  SELECT id FROM jobs WHERE status='pending' "
            "  AND (next_retry_at IS NULL OR next_retry_at <= ?)"
            f"{types_sql}"
            "  ORDER BY priority DESC, id ASC LIMIT 1"
            ") RETURNING id, asset_id, type, attempts, max_attempts",
            tuple(params),
        ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    def complete(self, job_id: int) -> None:
        self.db.execute(
            "UPDATE jobs SET status='done', finished_at=? WHERE id=?",
            (_now(), job_id),
        )

    def skip(self, job_id: int, reason: str = "") -> None:
        self.db.execute(
            "UPDATE jobs SET status='skipped', finished_at=?, error=? WHERE id=?",
            (_now(), reason, job_id),
        )

    def fail(self, job_id: int, error: str, backoff_base: float = 2.0) -> None:
        """失败重试（指数退避）；达到 max_attempts 则标记 failed。"""
        job = self.db.query_one(
            "SELECT attempts, max_attempts FROM jobs WHERE id=?", (job_id,)
        )
        if job is None:
            return
        attempts, max_attempts = job["attempts"], job["max_attempts"]
        if attempts >= max_attempts:
            self.db.execute(
                "UPDATE jobs SET status='failed', finished_at=?, error=? WHERE id=?",
                (_now(), error[:2000], job_id),
            )
            return
        delay = backoff_base * (2 ** (attempts - 1)) + random.uniform(0, 0.5)
        retry_at = utc_from_epoch(time.time() + delay)
        self.db.execute(
            "UPDATE jobs SET status='pending', next_retry_at=?, error=? WHERE id=?",
            (retry_at, error[:2000], job_id),
        )

    # ------------------------------------------------------------------
    def reset_running(self) -> int:
        """崩溃恢复：所有 running 任务恢复为 pending（可断点续扫）。"""
        cur = self.db.execute(
            "UPDATE jobs SET status='pending', started_at=NULL "
            "WHERE status='running'"
        )
        return cur.rowcount

    def retry_due_count(self, now: float | None = None) -> int:
        now_iso = _now()
        return int(
            self.db.scalar(
                "SELECT COUNT(*) FROM jobs WHERE status='pending' "
                "AND (next_retry_at IS NULL OR next_retry_at <= ?)",
                (now_iso,),
            )
            or 0
        )

    def stats(self) -> dict[str, int]:
        rows = self.db.query("SELECT status, COUNT(*) AS c FROM jobs GROUP BY status")
        return {r["status"]: r["c"] for r in rows}

    # ------------------------------------------------------------------
    def process_loop(self, handlers: dict[str, callable], job_types: Iterable[str] | None = None,
                     max_runs: int | None = None, sleep_sec: float = 0.2) -> int:
        """阻塞处理队列直到耗尽（或达到 max_runs）。返回处理数量。"""
        types = list(job_types) if job_types else list(handlers.keys())
        processed = 0
        while max_runs is None or processed < max_runs:
            job = self.claim(types)
            if job is None:
                break
            handler = handlers.get(job["type"])
            if handler is None:
                self.skip(job["id"], f"无处理器: {job['type']}")
                processed += 1
                continue
            try:
                handler(job)
                self.complete(job["id"])
            except Exception as exc:  # noqa: BLE001
                self.fail(job["id"], str(exc))
            processed += 1
            if sleep_sec > 0:
                time.sleep(sleep_sec)
        return processed
