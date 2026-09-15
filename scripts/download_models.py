#!/usr/bin/env python3
"""一次性下载离线模型到数据目录（仅首次安装需要网络；此后运行时零网络）。

用法：
    python3 scripts/download_models.py --data-dir ~/SnapVault
    python3 scripts/download_models.py --data-dir ~/SnapVault --embedding-only

- OCR 模型（det/rec/cls）随 snapvault-core 包分发（core/snapvault/models/rapidocr）
- Embedding 模型（BAAI/bge-small-zh-v1.5, ONNX）下载到 <data_dir>/models/fastembed
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def ensure_data_root(data_dir: str | Path) -> Path:
    root = Path(data_dir).expanduser().resolve()
    for sub in ("models", "db", "images", "thumbnails", "logs", "backups", "exports"):
        (root / sub).mkdir(parents=True, exist_ok=True)
    return root


def download_embedding(data_dir: str | Path) -> str:
    root = ensure_data_root(data_dir)
    cache_dir = root / "models" / "fastembed"
    print(f"[1/1] 下载 embedding 模型 BAAI/bge-small-zh-v1.5 → {cache_dir} ...")
    from fastembed import TextEmbedding

    model = TextEmbedding(model_name="BAAI/bge-small-zh-v1.5",
                          cache_dir=str(cache_dir), threads=0)
    vec = model.embed(["验证"]).__next__()
    print(f"  完成（维度 {len(vec)}），一次下载，此后完全离线推理。")
    return str(cache_dir)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description="SnapVault 离线模型下载")
    p.add_argument("--data-dir", default=None,
                   help="数据根目录（默认 $SNAPVAULT_DATA_DIR 或 ~/SnapVault）")
    p.add_argument("--embedding-only", action="store_true",
                   help="仅下载 embedding 模型（OCR 模型已随包分发）")
    args = p.parse_args(argv)
    try:
        download_embedding(args.data_dir)
    except Exception as exc:  # noqa: BLE001
        print(f"下载失败: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
