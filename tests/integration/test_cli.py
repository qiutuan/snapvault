"""CLI 端到端测试：进程内调用覆盖全部核心子命令（覆盖 cli.py）。

除 serve 使用子进程冒烟外，其余均以 in-process 方式调用 main()，
保证 pytest-cov 能统计到 cli.py 的执行路径。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

from snapvault.cli import main

CORE_DIR = Path(__file__).resolve().parents[2] / "core"


@pytest.fixture()
def cli_root(tmp_path: Path, fastembed_cache: Path) -> Path:
    root = tmp_path / "cli"
    models = root / "models"
    models.mkdir(parents=True)
    try:
        (models / "fastembed").symlink_to(fastembed_cache, target_is_directory=True)
    except OSError:
        shutil.copytree(fastembed_cache, models / "fastembed")
    os.environ["SNAPVAULT_DATA_DIR"] = str(root)
    yield root
    os.environ.pop("SNAPVAULT_DATA_DIR", None)


def _run(args: list[str]):
    return main(args)


class TestCliFlow:
    def test_full_flow(self, cli_root: Path, make_shot, capsys):
        shot = make_shot("订单支付成功 金额128元 等待发货", index=7)
        assert _run(["init"]) == 0
        assert _run(["import", str(shot), "--source", "cli-it"]) == 0
        assert _run(["process"]) == 0
        capsys.readouterr()  # 丢弃 init/import/process 输出

        assert _run(["search", "支付成功", "--json"]) == 0
        hits = json.loads(capsys.readouterr().out)
        assert hits and hits[0]["asset_id"] == 1

        capsys.readouterr()
        assert _run(["stats", "--json"]) == 0
        assert _run(["tag", "set", "1", "项目A/登录页"]) == 0
        assert _run(["tag", "list"]) == 0
        assert "项目A/登录页" in capsys.readouterr().out
        assert _run(["note", "set", "1", "这是备注"]) == 0
        capsys.readouterr()
        assert _run(["note", "get", "1"]) == 0
        assert "这是备注" in capsys.readouterr().out
        assert _run(["field", "define", "defect_id", "缺陷ID", "--type", "text"]) == 0
        assert _run(["field", "set", "1", "defect_id", "BUG-1"]) == 0
        capsys.readouterr()
        assert _run(["field", "get", "1"]) == 0
        assert "BUG-1" in capsys.readouterr().out

        anno_file = cli_root / "anno.json"
        anno_file.write_text('{"elements":[]}')
        assert _run(["annotate", "save", "1", str(anno_file)]) == 0
        assert _run(["annotate", "get", "1"]) == 0

        assert _run(["export", "--include", "annotated", "--pdf", "--out", str(cli_root / "exp")]) == 0
        assert (cli_root / "exp").exists()
        assert _run(["backup"]) == 0
        assert _run(["backups"]) == 0
        assert "snapshot" in capsys.readouterr().out or "backup" in capsys.readouterr().out
        assert _run(["rebuild"]) == 0
        assert _run(["privacy-check", "--json"]) == 0
        assert _run(["verify-integrity"]) == 0

        assert _run(["trash", "move", "1"]) == 0
        assert _run(["trash", "restore", "1"]) == 0
        mig = str(cli_root / "migration.zip")
        assert _run(["export-library", "--out", mig]) == 0
        target = cli_root / "new-root"
        assert _run(["import-library", mig, "--target", str(target)]) == 0
        assert _run(["version"]) == 0

    def test_search_no_result_and_similar(self, cli_root: Path, make_shot):
        shot = make_shot("API 网关 502 Bad Gateway 上游超时", index=8)
        assert _run(["import", str(shot)]) == 0
        assert _run(["process"]) == 0
        assert _run(["search", "不存在的词xyz", "--json"]) in (0, None)
        assert _run(["similar", "1", "--json"]) == 0

    def test_import_dir_incremental(self, cli_root: Path, make_shot):
        d = cli_root / "batch"
        d.mkdir()
        for i in range(3):
            p = make_shot(f"消息队列积压 消费者处理缓慢 {i}", index=i)
            shutil.copy(p, d / f"q{i}.png")
        assert _run(["import", str(d), "--dir", "--process"]) == 0
        assert _run(["stats", "--json"]) == 0


def test_serve_smoke(cli_root: Path):
    """serve 子命令子进程冒烟：启动→health→退出。"""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    env = dict(os.environ)
    env["PYTHONPATH"] = str(CORE_DIR)
    proc = subprocess.Popen(
        [sys.executable, "-m", "snapvault.cli", "serve", "--port", str(port)],
        cwd=str(CORE_DIR.parent), env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        ok = False
        for _ in range(20):
            time.sleep(0.3)
            try:
                import urllib.request
                with urllib.request.urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as r:
                    ok = json.loads(r.read()) == {"ok": True}
                    break
            except Exception:
                continue
        assert ok, "serve 未在 6s 内就绪"
    finally:
        proc.terminate()
        proc.wait(timeout=5)
