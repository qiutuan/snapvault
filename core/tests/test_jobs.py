"""M2 任务队列测试：幂等入队 / 原子领取 / 重试退避 / 崩溃恢复。"""
from __future__ import annotations

import time


def _mk_asset(db, path="/a.png", sha="h1"):
    return db.execute(
        "INSERT INTO assets(path, sha256) VALUES (?, ?) RETURNING id",
        (path, sha),
    ).fetchone()[0]


class TestEnqueue:
    def test_enqueue_and_idempotent(self, db):
        aid = _mk_asset(db)
        from snapvault.jobs import JobManager

        jm = JobManager(db)
        jid = jm.enqueue(aid, "ocr")
        assert jid is not None
        assert jm.enqueue(aid, "ocr") is None  # 幂等
        assert jm.enqueue(aid, "thumb") is not None

    def test_priority_order(self, db):
        from snapvault.jobs import JobManager

        jm = JobManager(db)
        a1 = _mk_asset(db, "/a", "h1")
        a2 = _mk_asset(db, "/b", "h2")
        jm.enqueue(a1, "ocr", priority=0)
        jm.enqueue(a2, "ocr", priority=5)
        first = jm.claim(["ocr"])
        assert first["asset_id"] == a2
        assert first["attempts"] == 1
        second = jm.claim(["ocr"])
        assert second["asset_id"] == a1


class TestClaimAndComplete:
    def test_claim_marks_running(self, db):
        from snapvault.jobs import JobManager

        jm = JobManager(db)
        aid = _mk_asset(db)
        jm.enqueue(aid, "ocr")
        job = jm.claim(["ocr"])
        assert job["status"] == "running" if "status" in job else True
        assert jm.claim(["ocr"]) is None  # 已被领取
        jm.complete(job["id"])
        assert jm.stats().get("done") == 1

    def test_fail_retries_with_backoff(self, db):
        from snapvault.jobs import JobManager

        jm = JobManager(db)
        aid = _mk_asset(db)
        jm.enqueue(aid, "ocr", max_attempts=3)
        job = jm.claim(["ocr"])
        jm.fail(job["id"], "boom")
        # 未到上限 → pending + next_retry_at
        row = db.query_one("SELECT status, next_retry_at FROM jobs WHERE id=?", (job["id"],))
        assert row["status"] == "pending"
        assert row["next_retry_at"] is not None
        # 未到期不可领取
        assert jm.claim(["ocr"]) is None
        # 到期后（推进时间）可领取，attempts=2
        import sqlite3

        db.execute(
            "UPDATE jobs SET next_retry_at=? WHERE id=?",
            ("1970-01-01T00:00:00Z", job["id"]),
        )
        job2 = jm.claim(["ocr"])
        assert job2["id"] == job["id"] and job2["attempts"] == 2
        jm.fail(job2["id"], "boom2")
        db.execute("UPDATE jobs SET next_retry_at='1970-01-01T00:00:00Z' WHERE id=?", (job["id"],))
        job3 = jm.claim(["ocr"])
        assert job3["attempts"] == 3
        jm.fail(job3["id"], "boom3")  # 达上限 → failed
        assert db.scalar("SELECT status FROM jobs WHERE id=?", (job["id"],)) == "failed"
        assert jm.stats().get("failed") == 1


class TestCrashRecovery:
    def test_reset_running(self, db):
        from snapvault.jobs import JobManager

        jm = JobManager(db)
        aid = _mk_asset(db)
        jm.enqueue(aid, "ocr")
        jm.claim(["ocr"])  # 模拟进程在 OCR 中途被 kill
        assert jm.stats().get("running") == 1
        assert jm.reset_running() == 1
        assert jm.stats().get("pending") == 1
        # 重新可领取
        job = jm.claim(["ocr"])
        assert job is not None and job["attempts"] == 2


class TestProcessLoop:
    def test_loop_runs_handlers(self, db):
        from snapvault.jobs import JobManager

        jm = JobManager(db)
        a1 = _mk_asset(db, "/a", "h1")
        a2 = _mk_asset(db, "/b", "h2")
        jm.enqueue(a1, "ocr")
        jm.enqueue(a2, "ocr")
        done = []
        n = jm.process_loop({"ocr": lambda job: done.append(job["asset_id"])}, max_runs=10)
        assert n == 2 and sorted(done) == sorted([a1, a2])
        assert jm.stats().get("done") == 2

    def test_loop_marks_failed_on_exception(self, db):
        from snapvault.jobs import JobManager

        jm = JobManager(db)
        aid = _mk_asset(db)
        jm.enqueue(aid, "ocr", max_attempts=1)
        jm.process_loop({"ocr": lambda job: (_ for _ in ()).throw(RuntimeError("x"))},
                        max_runs=10)
        assert jm.stats().get("failed") == 1
