"""统一 UTC 时间戳：YYYY-MM-DDTHH:MM:SS.ffffffZ（定宽，字典序即时间序）。"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

UTC_FORMAT = "%Y-%m-%dT%H:%M:%S.%fZ"


def utc_now() -> str:
    return datetime.now(timezone.utc).strftime(UTC_FORMAT)


def utc_from_epoch(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, tz=timezone.utc).strftime(UTC_FORMAT)


def utc_ago(days: float = 0, seconds: float = 0) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days, seconds=seconds)
            ).strftime(UTC_FORMAT)
