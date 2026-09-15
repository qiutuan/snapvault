"""截图采集（M3）：mss 跨屏全屏/区域截图，落盘走导入去重。

- 全屏：--monitor 0 表示所有屏幕拼图，N 表示第 N 块屏幕
- 区域：--region X,Y,W,H（像素坐标，基于 --monitor 指定屏幕）
- 落盘命名 {date}_{time}_{source}.{ext}，随后可交给 Importer 入库
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import Config
from .util import atomic_write

logger = logging.getLogger("snapvault.screenshot")


@dataclass
class ShotResult:
    path: str
    monitor: int
    width: int
    height: int


def _stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def capture(images_dir: str | Path, monitor: int = 0,
            region: tuple[int, int, int, int] | None = None,
            source: str = "screen") -> ShotResult:
    """mss 截图并原子落盘。monitor=0 表示所有屏幕拼接。"""
    import mss

    images_dir = Path(images_dir)
    images_dir.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        monitors = sct.monitors  # [0] 是所有屏幕拼接
        if monitor >= len(monitors):
            raise ValueError(f"屏幕编号 {monitor} 超出范围（共 {len(monitors)-1} 块）")
        mon = monitors[monitor]
        if region:
            x, y, w, h = region
            mon = {"left": mon["left"] + x, "top": mon["top"] + y, "width": w, "height": h}
        shot = sct.grab(mon)
        from PIL import Image

        img = Image.frombytes("RGB", shot.size, shot.rgb)
        path = images_dir / f"{_stamp()}_{source}.png"
        atomic_write(path, _png_bytes(img))
        logger.info("screenshot.captured path=%s size=%sx%s", path, img.width, img.height)
        return ShotResult(str(path), monitor, img.width, img.height)


def _png_bytes(img) -> bytes:
    from io import BytesIO

    bio = BytesIO()
    img.save(bio, "PNG")
    return bio.getvalue()
