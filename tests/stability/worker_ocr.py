"""稳定性 worker：导入 N 张图 → 循环处理 OCR 任务（提交前睡眠供父进程 kill）。"""
from __future__ import annotations

import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "core"))


def main() -> int:
    from snapvault.engine import SnapVault

    images = [Path(p) for p in sys.argv[1:]]
    engine = SnapVault(acquire_lock=False, setup_logs=False)
    rep = engine.import_files(images, source="stability")
    (engine.config.root / ".worker_ready").write_text("ready\n")
    print(f"imported={len(rep.imported)}", flush=True)
    # 处理循环：OCR 提交前睡眠，暴露 kill 窗口
    handled = 0
    while True:
        job = engine.jobs.claim(["ocr"])
        if job is None:
            break
        sleep_ms = int(os.environ.get("SNAPVAULT_TEST_SLEEP_MS", "0"))
        if sleep_ms:
            time.sleep(sleep_ms / 1000.0)
        from snapvault.pipeline import ocr_handler

        try:
            ocr_handler(engine.db, engine.config, engine.ocr)(job)
            engine.jobs.complete(job["id"])
        except Exception as exc:  # noqa: BLE001
            engine.jobs.fail(job["id"], str(exc))
        handled += 1
    print(f"handled={handled}", flush=True)
    engine.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
