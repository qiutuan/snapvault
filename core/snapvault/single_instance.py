"""单实例锁：防止多开损坏数据库。

POSIX 使用 fcntl.flock；Windows 使用 msvcrt 文件锁；均以数据目录锁文件为锚点。
"""
from __future__ import annotations

import os
from pathlib import Path

from .exceptions import SingleInstanceError

try:  # POSIX
    import fcntl

    _HAS_FCNTL = True
except ImportError:  # pragma: no cover - windows
    _HAS_FCNTL = False

try:
    import msvcrt

    _HAS_MSVCRT = True
except ImportError:  # pragma: no cover - posix
    _HAS_MSVCRT = False


class SingleInstanceLock:
    """持有数据目录的全局锁；acquire 失败即已有实例在运行。"""

    def __init__(self, data_root: str | os.PathLike):
        self.lock_path = Path(data_root) / ".snapvault.lock"
        self.lock_path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = None

    def acquire(self) -> None:
        self._fh = open(self.lock_path, "a+", encoding="utf-8")
        try:
            if _HAS_FCNTL:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            elif _HAS_MSVCRT:  # pragma: no cover - windows
                import msvcrt as _m

                _m.locking(self._fh.fileno(), _m.LK_NBLCK, 1)
            else:  # pragma: no cover
                raise SingleInstanceError("当前平台不支持文件锁")
        except (BlockingIOError, OSError) as exc:
            self._fh.close()
            self._fh = None
            raise SingleInstanceError(
                f"SnapVault 已有实例在运行（锁文件: {self.lock_path}），请勿多开以免损坏数据库。"
            ) from exc
        # 写入 pid 便于诊断
        self._fh.seek(0)
        self._fh.truncate()
        self._fh.write(str(os.getpid()))

    def release(self) -> None:
        if self._fh is None:
            return
        try:
            if _HAS_FCNTL:
                fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
            elif _HAS_MSVCRT:  # pragma: no cover
                import msvcrt as _m

                self._fh.seek(0)
                _m.locking(self._fh.fileno(), _m.LK_UNLCK, 1)
        finally:
            self._fh.close()
            self._fh = None

    def __enter__(self) -> "SingleInstanceLock":
        self.acquire()
        return self

    def __exit__(self, *exc) -> None:
        self.release()
