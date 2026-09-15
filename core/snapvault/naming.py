"""截图文件名规范：YYYYMMDD_HHmmss_来源.{ext}，支持用户自定义模板。

模板令牌：
  {date}      -> YYYYMMDD
  {time}      -> HHmmss
  {datetime}  -> YYYYMMDD_HHmmss
  {source}    -> 来源（capture/clipboard/import/dragdrop/自定义）
  {ext}       -> 扩展名（不含点）
  {seq}       -> 序号（同秒冲突时自动追加 _2、_3 …）
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from .util import sanitize_filename

DEFAULT_TEMPLATE = "{date}_{time}_{source}.{ext}"

_SAFE_SOURCE = re.compile(r"[^A-Za-z0-9_\-\u4e00-\u9fff]")


def normalize_source(source: str) -> str:
    s = _SAFE_SOURCE.sub("_", source or "import").strip("_")
    return s or "import"


def render_filename(
    template: str,
    when: datetime | None = None,
    source: str = "import",
    ext: str = "png",
    seq: int = 0,
) -> str:
    """按模板渲染文件名（不含路径）。"""
    when = when or datetime.now(timezone.utc)
    tokens = {
        "date": when.strftime("%Y%m%d"),
        "time": when.strftime("%H%M%S"),
        "datetime": when.strftime("%Y%m%d_%H%M%S"),
        "source": normalize_source(source),
        "ext": (ext or "png").lstrip(".").lower(),
        "seq": str(seq) if seq > 0 else "",
    }

    def _sub(m: re.Match) -> str:
        return tokens.get(m.group(1), m.group(0))

    rendered = re.sub(r"\{(\w+)\}", _sub, template)
    # 同秒/重名冲突时，在扩展名前追加序号 _N（与模板无关的自动消歧）
    if seq > 0:
        p = Path(rendered)
        rendered = f"{p.stem}_{seq}{p.suffix}"
    rendered = re.sub(r"_+$", "", rendered).strip()
    rendered = rendered.replace(" ", "_")
    return sanitize_filename(rendered)


def unique_filename(
    directory: str | Path,
    template: str,
    when: datetime | None = None,
    source: str = "import",
    ext: str = "png",
) -> str:
    """生成目录内唯一文件名，同秒冲突追加序号。"""
    directory = Path(directory)
    base = render_filename(template, when, source, ext)
    candidate = base
    seq = 1
    while (directory / candidate).exists():
        candidate = render_filename(template, when, source, ext, seq)
        seq += 1
        if seq > 100000:  # 防御
            raise RuntimeError("无法生成唯一文件名")
    return candidate
