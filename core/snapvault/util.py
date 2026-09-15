"""通用工具：原子写盘、纯函数辅助。"""
from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

SUPPORTED_IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".gif"}


def is_supported_image(path: str | os.PathLike) -> bool:
    return Path(path).suffix.lower() in SUPPORTED_IMAGE_EXTS


def sha256_file(path: str | os.PathLike, chunk_size: int = 1 << 20) -> str:
    """流式计算文件 SHA-256。"""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while True:
            chunk = f.read(chunk_size)
            if not chunk:
                break
            h.update(chunk)
    return h.hexdigest()


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def atomic_write(path: str | os.PathLike, data: bytes, mode: int = 0o644) -> None:
    """原子写盘：同目录临时文件 + fsync + rename。"""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=".part")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def atomic_write_text(path: str | os.PathLike, text: str, mode: int = 0o644) -> None:
    atomic_write(path, text.encode("utf-8"), mode)


def ensure_dir(path: str | os.PathLike) -> Path:
    p = Path(path)
    p.mkdir(parents=True, exist_ok=True)
    return p


def human_size(num_bytes: int) -> str:
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f}{unit}" if unit != "B" else f"{int(size)}B"
        size /= 1024
    return f"{num_bytes}B"


def sanitize_filename(name: str) -> str:
    """去除文件名中的非法字符。"""
    bad = '<>:"/\\|?*\x00'
    out = []
    for ch in name:
        out.append("_" if ch in bad else ch)
    s = "".join(out).strip(" .")
    return s or "untitled"
