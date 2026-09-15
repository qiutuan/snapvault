"""M9 隐私自检测试：连接快照 / 出站计数 / 静态无网络 import。"""
from __future__ import annotations

from snapvault.privacy import NetworkProbe, startup_network_check


class TestNetworkProbe:
    def test_snapshot_shape(self):
        conns = NetworkProbe.snapshot()
        assert isinstance(conns, list)
        for c in conns:
            assert isinstance(c, dict)

    def test_scan_passes_locally(self):
        """本地执行一轮轻量活动后，不应出现新增出站连接。"""
        report = NetworkProbe().scan(activity=lambda: _noop_local_work())
        assert report.passed is True
        for check in report.checks:
            assert check["passed"] is True

    def test_static_no_network_imports(self):
        report = NetworkProbe().scan()
        check = next(c for c in report.checks
                     if c["name"] == "core_static_no_network_imports")
        assert check["passed"] is True

    def test_outbound_count(self):
        assert NetworkProbe.outbound_count([]) == 0
        assert NetworkProbe.outbound_count(
            [{"raddr": "1.2.3.4:80", "status": "ESTABLISHED"}]) == 1
        assert NetworkProbe.outbound_count(
            [{"raddr": None, "status": "ESTABLISHED"}]) == 0
        assert NetworkProbe.outbound_count(
            [{"raddr": "1.2.3.4:80", "status": "LISTEN"}]) == 0


def _noop_local_work() -> None:
    """本地活动：纯计算，不建立任何网络 socket。"""
    total = sum(i * i for i in range(1000))
    assert total > 0
