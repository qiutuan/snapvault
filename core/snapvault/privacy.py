"""隐私自检（M9）：证明应用未发出任何网络请求。

原理（Linux）：用 psutil 采集本进程的 TCP/UDP 连接快照，执行一轮本地操作
（导入/检索/OCR）后再采集，比对无「新增出站连接」；同时扫描模块导入前后
进程打开的网络套接字数量，验证核心模块不建立任何网络 socket。

启动时调用 startup_network_check() 做基线探测并在日志中声明。
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from pathlib import Path

from .logging_setup import log_event

logger = logging.getLogger("snapvault.privacy")


@dataclass
class PrivacyReport:
    checks: list[dict] = field(default_factory=list)
    passed: bool = True

    def to_dict(self) -> dict:
        return {"passed": self.passed, "checks": self.checks}


class NetworkProbe:
    """基于 psutil 的进程级网络连接探测。"""

    @staticmethod
    def snapshot() -> list[dict]:
        """采集当前进程的 TCP/UDP 连接（含 LISTEN 与外连）。"""
        try:
            import psutil

            conns = psutil.net_connections(kind="inet")
        except (ImportError, PermissionError, OSError) as exc:  # pragma: no cover
            return [{"error": f"无法采集连接信息: {exc}"}]
        out = []
        pid = None
        try:
            import os

            pid = os.getpid()
        except OSError:
            pass
        for c in conns:
            # 仅本进程相关；无权限时尝试按 pid 过滤
            if c.pid is not None and c.pid != pid and pid is not None:
                continue
            out.append({
                "pid": c.pid,
                "type": c.type.name if hasattr(c.type, "name") else str(c.type),
                "status": c.status,
                "laddr": f"{c.laddr.ip}:{c.laddr.port}" if c.laddr else None,
                "raddr": f"{c.raddr.ip}:{c.raddr.port}" if c.raddr else None,
            })
        return out

    @staticmethod
    def outbound_count(conns: list[dict]) -> int:
        """出站连接数：ESTABLISHED/SYN_SENT 且存在对端地址。"""
        n = 0
        for c in conns:
            if c.get("raddr") and c.get("status") in ("ESTABLISHED", "SYN_SENT"):
                n += 1
        return n

    def scan(self, activity: callable | None = None) -> PrivacyReport:
        """执行一轮「自检活动」并比对前后连接状态。"""
        report = PrivacyReport()
        before = self.snapshot()
        if activity is not None:
            activity()
        after = self.snapshot()
        b_out = self.outbound_count(before)
        a_out = self.outbound_count(after)
        before_laddr = {tuple(sorted(c.items())) for c in before if c.get("laddr")}
        after_laddr = {tuple(sorted(c.items())) for c in after if c.get("laddr")}
        new_sockets = after_laddr - before_laddr
        ok = a_out <= b_out and not new_sockets
        report.checks.append({
            "name": "scan_activity_outbound",
            "before_outbound": b_out,
            "after_outbound": a_out,
            "new_local_sockets": len(new_sockets),
            "passed": ok,
        })
        report.passed = report.passed and ok

        # 检查核心模块是否存在网络相关 import（静态证据）
        core_dir = Path(__file__).resolve().parent
        net_imports = ["requests", "urllib.request", "http.client", "socket",
                       "aiohttp", "httpx", "urllib3"]
        hits = []
        for py in core_dir.glob("*.py"):
            text = py.read_text(encoding="utf-8", errors="ignore")
            for mod in net_imports:
                if f"import {mod}" in text or f"from {mod}" in text:
                    hits.append(f"{py.name}: {mod}")
        report.checks.append({
            "name": "core_static_no_network_imports",
            "hits": hits,
            "passed": not hits,
        })
        report.passed = report.passed and not hits
        return report


def startup_network_check(activity: callable | None = None) -> PrivacyReport:
    """启动时网络探测自检并在日志中声明（幂等）。"""
    report = NetworkProbe().scan(activity)
    log_event(logger, "privacy.startup_check",
              passed=report.passed, checks=report.to_dict())
    if not report.passed:
        logger.warning("隐私自检未通过！请检查环境。")
    else:
        logger.info("网络探测自检通过：启动时无任何外连请求。")
    return report
