"""单实例锁（single_instance.py）与截图采集（screenshot.py，mss 模拟）测试。"""
from __future__ import annotations

from pathlib import Path

import pytest

from snapvault.single_instance import SingleInstanceLock


class TestSingleInstance:
    def test_acquire_release(self, tmp_path: Path):
        lock = SingleInstanceLock(tmp_path)
        lock.acquire()
        lock.release()
        # 释放后可再次获取
        lock.acquire()
        lock.release()

    def test_second_instance_denied(self, tmp_path: Path):
        lock1 = SingleInstanceLock(tmp_path)
        lock1.acquire()
        with pytest.raises(Exception):
            SingleInstanceLock(tmp_path).acquire()
        lock1.release()

    def test_context_manager(self, tmp_path: Path):
        with SingleInstanceLock(tmp_path):
            with pytest.raises(Exception):
                SingleInstanceLock(tmp_path).acquire()


class TestScreenshotCapture:
    """mock mss：覆盖 capture() 的全屏/区域/原子落盘逻辑（无头环境无真实屏幕）。"""

    @pytest.fixture()
    def fake_mss(self, monkeypatch):
        class FakeGrab:
            def __init__(self, mon):
                self.size = (mon["width"], mon["height"])
                w, h = self.size
                # 生成一张纯灰底图，模拟屏幕内容
                self.rgb = bytes(w * h * 3)  # 黑
                for i in range(min(64, w * h)):
                    self.rgb = bytearray(self.rgb)
                    self.rgb[i * 3] = 200
                    self.rgb[i * 3 + 1] = 200
                    self.rgb[i * 3 + 2] = 200
                    self.rgb = bytes(self.rgb)

        class FakeSCT:
            monitors = [
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
                {"left": 0, "top": 0, "width": 1920, "height": 1080},
                {"left": 1920, "top": 0, "width": 1280, "height": 720},
            ]

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

            def grab(self, mon):
                return FakeGrab(mon)

        import sys
        import types

        mod = types.ModuleType("mss")
        mod.mss = lambda: FakeSCT()
        monkeypatch.setitem(sys.modules, "mss", mod)

    def test_fullscreen(self, tmp_path: Path, fake_mss):
        from snapvault.screenshot import capture

        r = capture(tmp_path, monitor=1, source="screen")
        p = Path(r.path)
        assert p.exists() and p.suffix == ".png"
        assert r.width == 1920 and r.height == 1080

    def test_region(self, tmp_path: Path, fake_mss):
        from snapvault.screenshot import capture

        r = capture(tmp_path, monitor=2, region=(100, 50, 800, 600), source="screen")
        assert Path(r.path).exists()
        assert r.width == 800 and r.height == 600

    def test_monitor_out_of_range(self, tmp_path: Path, fake_mss):
        from snapvault.screenshot import capture

        with pytest.raises(ValueError):
            capture(tmp_path, monitor=9)
