"""core 层测试公共夹具：隔离的临时数据目录与数据库。"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

import pytest

from snapvault.config import Config
from snapvault.db import Database


@pytest.fixture()
def data_root(tmp_path: Path) -> Path:
    root = tmp_path / "data"
    root.mkdir()
    return root


@pytest.fixture()
def config(data_root: Path) -> Config:
    return Config(data_root)


@pytest.fixture()
def db_path(data_root: Path) -> Path:
    return data_root / "db" / "snapvault.db"


@pytest.fixture()
def db(db_path: Path) -> Database:
    database = Database(db_path)
    yield database
    database.close()


@pytest.fixture()
def asset(db) -> int:
    return db.execute(
        "INSERT INTO assets(path, sha256) VALUES ('/asset.png', 'hash1') RETURNING id"
    ).fetchone()[0]


@pytest.fixture()
def make_image(tmp_path: Path):
    """生成一张带文字的测试图片，返回 (path, text)。"""
    from PIL import Image, ImageDraw, ImageFont

    font_path = Path(__file__).resolve().parents[2] / "fonts" / "NotoSansSC-Regular.otf"

    def _make(text: str = "测试文本 hello", size: tuple[int, int] = (640, 360),
              color: str = "white", fg: str = "black") -> Path:
        img = Image.new("RGB", size, color)
        draw = ImageDraw.Draw(img)
        try:
            font = ImageFont.truetype(str(font_path), 28)
        except OSError:
            font = ImageFont.load_default()
        draw.text((40, 40), text, fill=fg, font=font)
        p = tmp_path / f"img_{abs(hash(text))}.png"
        img.save(p)
        return p

    return _make
