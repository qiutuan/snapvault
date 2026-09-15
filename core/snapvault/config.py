"""数据目录与配置管理。

单一数据目录原则：所有数据（图片 / 数据库 / 索引 / 配置 / 日志 / 模型）
集中在一个用户可配置根目录下，支持整体迁移。

优先级：
1. 环境变量 SNAPVAULT_DATA_DIR
2. 用户显式指定（首次启动引导 / CLI --data-dir）
3. 默认 ~/SnapVault
"""
from __future__ import annotations

import json
import os
import threading
from pathlib import Path

from .exceptions import ConfigError

DEFAULT_DATA_DIR_NAME = "SnapVault"
ENV_DATA_DIR = "SNAPVAULT_DATA_DIR"
CONFIG_FILE = "config.json"

# 目录结构常量
IMAGES_DIR = "images"          # 原图（只读，永不修改）
THUMBS_DIR = "thumbnails"
DB_DIR = "db"
LOGS_DIR = "logs"
MODELS_DIR = "models"
BACKUPS_DIR = "backups"
EXPORTS_DIR = "exports"

_DEFAULT_CONFIG: dict = {
    "version": 1,
    "naming_template": "{date}_{time}_{source}.{ext}",  # YYYYMMDD_HHmmss_来源.{ext}
    "ocr_enabled": True,
    "embedding_enabled": True,
    "notes_in_fts": True,
    "ocr_lang": "ch+en",
    "max_workers": 0,             # 0 = 自动（CPU 核数 - 1）
    "hotkeys": {
        "fullscreen": "Ctrl+Shift+1",
        "region": "Ctrl+Shift+2",
    },
    "recycle_bin_days": 30,
    "backup_enabled": True,
    "backup_keep": 7,
    "encryption_enabled": False,
}


class Config:
    """持有数据根目录，并读写其中的 config.json。线程安全。"""

    def __init__(self, root_dir: str | os.PathLike | None = None, create: bool = True):
        self._lock = threading.RLock()
        if root_dir is None:
            root_dir = os.environ.get(ENV_DATA_DIR) or str(
                Path.home() / DEFAULT_DATA_DIR_NAME
            )
        self.root = Path(root_dir).expanduser().resolve()
        if create:
            self._ensure_layout()
        self._data = self._load()

    # ------------------------------------------------------------------
    def _ensure_layout(self) -> None:
        for sub in (
            IMAGES_DIR, THUMBS_DIR, DB_DIR, LOGS_DIR, MODELS_DIR,
            BACKUPS_DIR, EXPORTS_DIR,
        ):
            (self.root / sub).mkdir(parents=True, exist_ok=True)
        (self.root / ".snapvault_root").write_text("SnapVault data root v1\n", encoding="utf-8")

    def _load(self) -> dict:
        cfg_path = self.root / CONFIG_FILE
        data = dict(_DEFAULT_CONFIG)
        if cfg_path.exists():
            try:
                loaded = json.loads(cfg_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError) as exc:
                raise ConfigError(f"配置文件损坏: {cfg_path}: {exc}") from exc
            if not isinstance(loaded, dict):
                raise ConfigError(f"配置文件格式错误: {cfg_path}")
            data.update(loaded)
        return data

    def save(self) -> None:
        """原子化写回 config.json（临时文件 + rename）。"""
        with self._lock:
            cfg_path = self.root / CONFIG_FILE
            tmp = cfg_path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
            os.replace(tmp, cfg_path)

    # ------------------------------------------------------------------
    # 路径
    def images_dir(self) -> Path:
        return self.root / IMAGES_DIR

    def thumbnails_dir(self) -> Path:
        return self.root / THUMBS_DIR

    def db_path(self) -> Path:
        return self.root / DB_DIR / "snapvault.db"

    def models_dir(self) -> Path:
        return self.root / MODELS_DIR

    def backups_dir(self) -> Path:
        return self.root / BACKUPS_DIR

    def exports_dir(self) -> Path:
        return self.root / EXPORTS_DIR

    def log_path(self) -> Path:
        return self.root / LOGS_DIR / "snapvault.log"

    # ------------------------------------------------------------------
    # 键值读写
    def get(self, key: str, default=None):
        with self._lock:
            return self._data.get(key, default)

    def set(self, key: str, value) -> None:
        with self._lock:
            self._data[key] = value
            self.save()

    def set_many(self, mapping: dict) -> None:
        with self._lock:
            self._data.update(mapping)
            self.save()

    def as_dict(self) -> dict:
        with self._lock:
            return dict(self._data)

    @property
    def naming_template(self) -> str:
        return self.get("naming_template", _DEFAULT_CONFIG["naming_template"])

    @property
    def ocr_enabled(self) -> bool:
        return bool(self.get("ocr_enabled", True))

    @property
    def embedding_enabled(self) -> bool:
        return bool(self.get("embedding_enabled", True))

    @property
    def notes_in_fts(self) -> bool:
        return bool(self.get("notes_in_fts", True))

    @property
    def recycle_bin_days(self) -> int:
        return int(self.get("recycle_bin_days", 30))

    @property
    def backup_keep(self) -> int:
        return int(self.get("backup_keep", 7))

    @property
    def max_workers(self) -> int:
        v = int(self.get("max_workers", 0))
        if v <= 0:
            import os as _os
            v = max(1, (_os.cpu_count() or 2) - 1)
        return v
