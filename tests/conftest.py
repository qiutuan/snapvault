"""集成测试公共夹具：临时数据根（含 fastembed 模型软链）与完整引擎。"""
from __future__ import annotations

import shutil
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CORE_DIR = REPO_ROOT / "core"
FONTS = REPO_ROOT / "fonts" / "NotoSansSC-Regular.otf"
FASTEMBED_CACHE = REPO_ROOT / ".models-cache" / "models" / "fastembed"


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def font_path() -> Path:
    assert FONTS.exists(), f"中文字体缺失: {FONTS}"
    return FONTS


@pytest.fixture(scope="session")
def fastembed_cache() -> Path:
    if FASTEMBED_CACHE.exists():
        return FASTEMBED_CACHE
    # 无缓存时尝试用快照内的模型
    alt = REPO_ROOT / "app" / "resources" / "models" / "fastembed"
    if alt.exists():
        return alt
    pytest.skip("fastembed 模型缓存缺失，请先运行 scripts/download_models.py")


@pytest.fixture()
def data_dir(tmp_path: Path, fastembed_cache: Path) -> Path:
    """带模型软链的隔离数据根目录。"""
    root = tmp_path / "data"
    root.mkdir(parents=True)
    models = root / "models"
    models.mkdir()
    try:
        (models / "fastembed").symlink_to(fastembed_cache, target_is_directory=True)
    except OSError:
        shutil.copytree(fastembed_cache, models / "fastembed")
    return root


@pytest.fixture()
def engine(data_dir: Path):
    from snapvault.engine import SnapVault

    eng = SnapVault(data_dir, acquire_lock=False, setup_logs=False)
    yield eng
    eng.close()


@pytest.fixture()
def db_path(data_dir: Path) -> Path:
    return data_dir / "db" / "snapvault.db"


@pytest.fixture()
def make_shot(font_path: Path):
    """生成带指定文字的截图（白底黑字，便于 OCR）。"""
    from PIL import Image, ImageDraw, ImageFont

    def _make(text: str, size=(480, 240), index: int = 0) -> Path:
        from tempfile import NamedTemporaryFile

        img = Image.new("RGB", size, "white")
        d = ImageDraw.Draw(img)
        font = ImageFont.truetype(str(font_path), 30)
        d.text((30, 50), text, fill="black", font=font)
        d.rectangle((30, 150, size[0] - 30, 190), outline="gray", width=2)
        f = NamedTemporaryFile(suffix=".png", delete=False)
        f.close()
        img.save(f.name)
        return Path(f.name)

    return _make
