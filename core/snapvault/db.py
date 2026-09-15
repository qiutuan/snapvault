"""SQLite 主库访问层：WAL、外键、事务、sqlite-vec 扩展、迁移与完整性校验。"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

import sqlite_vec

from .exceptions import DatabaseError
from .schema import EMBEDDING_DIM, integrity_check, migrate

_WAL_PRAGMAS = """
PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;
PRAGMA synchronous = NORMAL;
PRAGMA busy_timeout = 30000;
"""


def open_database(db_path: str | Path) -> sqlite3.Connection:
    """打开（必要时创建）主库，启用 WAL/外键，加载 sqlite-vec，并完成迁移。"""
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
    try:
        conn.row_factory = sqlite3.Row
        # 关闭隐式事务：所有多步写入必须显式走 Database.tx()，保证原子性
        conn.isolation_level = None
        conn.execute("PRAGMA foreign_keys = ON")
        for stmt in _WAL_PRAGMAS.split(";"):
            stmt = stmt.strip()
            if stmt:
                conn.execute(stmt)
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
        migrate(conn)
        problems = integrity_check(conn)
        if problems:
            raise DatabaseError("数据库完整性检查失败: " + "; ".join(problems[:5]))
    except sqlite3.DatabaseError as exc:
        conn.close()
        raise DatabaseError(f"数据库损坏或无法打开: {exc}") from exc
    except BaseException:
        conn.close()
        raise
    return conn


class Database:
    """线程安全的数据库门面。所有多步写入通过 self.tx() 事务包裹。"""

    def __init__(self, db_path: str | Path):
        self.db_path = Path(db_path)
        self._local = threading.local()

    # ------------------------------------------------------------------
    def _conn(self) -> sqlite3.Connection:
        # 每个线程独立连接（WAL 支持并发读写）
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = open_database(self.db_path)
            self._local.conn = conn
        return conn

    def close(self) -> None:
        conn = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    # ------------------------------------------------------------------
    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        """事务上下文：正常退出提交，异常回滚。"""
        conn = self._conn()
        try:
            conn.execute("BEGIN")
            yield conn
            conn.execute("COMMIT")
        except BaseException:
            conn.execute("ROLLBACK")
            raise

    def execute(self, sql: str, params: tuple = ()) -> sqlite3.Cursor:
        return self._conn().execute(sql, params)

    def executemany(self, sql: str, seq: list[tuple]) -> sqlite3.Cursor:
        return self._conn().executemany(sql, seq)

    def query(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        return self._conn().execute(sql, params).fetchall()

    def query_one(self, sql: str, params: tuple = ()) -> sqlite3.Row | None:
        return self._conn().execute(sql, params).fetchone()

    def scalar(self, sql: str, params: tuple = ()):
        row = self._conn().execute(sql, params).fetchone()
        return row[0] if row else None

    # ------------------------------------------------------------------
    def integrity_ok(self) -> bool:
        return not integrity_check(self._conn())

    @property
    def embedding_dim(self) -> int:
        return EMBEDDING_DIM
