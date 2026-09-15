"""结构化日志：JSON 行格式，旋转，单文件 <=50MB，保留 5 个。"""
from __future__ import annotations

import json
import logging
import logging.handlers
import sys
from pathlib import Path

LOG_MAX_BYTES = 50 * 1024 * 1024
LOG_BACKUP_COUNT = 5

_RESERVED = {"asctime", "levelname", "name", "message", "module", "funcName", "lineno"}


class JsonFormatter(logging.Formatter):
    """输出 JSON 行日志，方便机器解析与审计留痕。"""

    def format(self, record: logging.LogRecord) -> str:
        payload = {
            "ts": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            "module": record.module,
            "func": record.funcName,
            "line": record.lineno,
            "msg": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        extra = getattr(record, "extra_fields", None)
        if isinstance(extra, dict):
            for k, v in extra.items():
                if k not in _RESERVED:
                    payload[k] = v
        return json.dumps(payload, ensure_ascii=False)


def _configure(root_logger: logging.Logger, log_path: Path | None) -> None:
    root_logger.setLevel(logging.INFO)
    root_logger.handlers.clear()
    if log_path is not None:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        fh = logging.handlers.RotatingFileHandler(
            log_path, maxBytes=LOG_MAX_BYTES, backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
        fh.setFormatter(JsonFormatter())
        root_logger.addHandler(fh)
    sh = logging.StreamHandler(sys.stderr)
    sh.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
    root_logger.addHandler(sh)


def setup_logging(log_path: Path | None = None, logger_name: str = "snapvault") -> logging.Logger:
    """初始化根日志（幂等：重复调用不会叠加 handler）。"""
    logger = logging.getLogger(logger_name)
    if getattr(logger, "_snapvault_configured", False):
        return logger
    _configure(logger, log_path)
    logger._snapvault_configured = True  # type: ignore[attr-defined]
    return logger


def log_event(logger: logging.Logger, action: str, **fields) -> None:
    """关键操作留痕（导入 / OCR / 删除 / 导出等）。"""
    logger.info(action, extra={"extra_fields": fields})
