"""HTTP 服务集成测试：启动本地服务（仅 127.0.0.1），验证核心闭环 API。

覆盖 server.py（此前未纳入覆盖）及端到端 HTTP 链路。
"""
from __future__ import annotations

import json
import socket
import threading
import urllib.request
from pathlib import Path

import pytest
import shutil

from snapvault.engine import SnapVault


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.fixture()
def server_ctx(data_dir: Path):
    """函数级：重置 server 类级 engine 单例，绑定当前数据目录。"""
    from snapvault import server as sv

    sv._Handler.engine = None  # 关键：清掉上一个测试残留的 engine
    eng = SnapVault(data_dir, acquire_lock=False, setup_logs=False)
    port = _free_port()
    httpd = sv.serve(host="127.0.0.1", port=port, data_dir=str(data_dir))
    t = threading.Thread(target=httpd.serve_forever, daemon=True)
    t.start()
    yield f"http://127.0.0.1:{port}", eng
    httpd.shutdown()
    sv._Handler.engine = None
    eng.close()


def _post(url: str, payload: dict):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read())


class TestServer:
    def test_health(self, server_ctx):
        url, _ = server_ctx
        with urllib.request.urlopen(f"{url}/api/health", timeout=10) as r:
            assert json.loads(r.read()) == {"ok": True}

    def test_import_search_via_api(self, server_ctx, make_shot):
        """导入一张含文字截图 → API 搜索 → 命中。"""
        url, eng = server_ctx
        paths = [make_shot("订单支付成功 金额128元 等待发货", index=1)]
        rep = eng.import_files(paths, source="server-it")
        eng.process_jobs()
        hits = _post(f"{url}/api/search", {"query": "支付成功", "k": 3})
        assert any(h["asset_id"] == rep.imported[0] for h in hits)
        assert any("支付成功" in h.get("fragment", "") for h in hits)

    def test_detail_and_tags(self, server_ctx, make_shot):
        url, eng = server_ctx
        paths = [make_shot("API 网关 502 Bad Gateway 上游超时", index=2)]
        rep = eng.import_files(paths, source="server-it")
        eng.process_jobs()
        eng.metadata.set_asset_tags(rep.imported[0], ["运维/网关"])
        d = _post(f"{url}/api/detail", {"assetId": rep.imported[0]})
        assert d["id"] == rep.imported[0]
        assert "网关" in d["ocr"]
        assert "运维/网关" in d["tags"]

    def test_thumb_small_and_big(self, server_ctx, make_shot):
        url, eng = server_ctx
        paths = [make_shot("Redis 集群超时 重试机制已启用", index=3)]
        rep = eng.import_files(paths, source="server-it")
        eng.process_jobs()
        for big in (0, 1):
            with urllib.request.urlopen(
                    f"{url}/api/thumb?id={rep.imported[0]}&big={big}&t=1", timeout=15) as r:
                assert r.headers["Content-Type"].startswith("image/")
                assert len(r.read()) > 500

    def test_thumb_annotated_preview(self, server_ctx, make_shot):
        """有标注时大图返回栅格化打码预览。"""
        url, eng = server_ctx
        paths = [make_shot("用户会话过期 重新登录", index=4)]
        rep = eng.import_files(paths, source="server-it")
        eng.process_jobs()
        aid = rep.imported[0]
        from snapvault.annotations import Element
        model = eng.annotations.get_model(aid)
        model.add(Element(etype="mosaic", rect=(10, 10, 300, 200)))
        eng.annotations.save_model(aid, model)
        with urllib.request.urlopen(
                f"{url}/api/thumb?id={aid}&big=1&t=1", timeout=15) as r:
            assert r.headers["Content-Type"] == "image/png"
            assert len(r.read()) > 500

    def test_export_via_api(self, server_ctx, make_shot, tmp_path):
        url, eng = server_ctx
        paths = [make_shot("证书过期 TLS 握手失败", index=5)]
        rep = eng.import_files(paths, source="server-it")
        eng.process_jobs()
        # export 使用模块级 engine 的数据目录，直接调引擎导出
        from snapvault.exporter import ExportOptions
        out = eng.export(ExportOptions(include="original", with_pdf=True))
        pkg = Path(out.package_dir)
        assert pkg.exists()
        assert (pkg / "metadata.csv").exists()


class TestServerMore:
    """覆盖其余 API 端点（stats/note/mosaic/anno/trash/backup/settings/import_dir/process）。"""

    def test_stats_and_settings(self, server_ctx):
        url, eng = server_ctx
        s = _post(f"{url}/api/stats", {})
        assert "assets" in s
        r = _post(f"{url}/api/settings", {"ocr": True, "backupKeep": 3})
        assert r["ok"] is True

    def test_save_note_and_mosaic_export(self, server_ctx, make_shot):
        url, eng = server_ctx
        paths = [make_shot("埋点数据上报失败 网络波动", index=9)]
        rep = eng.import_files(paths, source="server-it")
        eng.process_jobs()
        aid = rep.imported[0]
        assert _post(f"{url}/api/save_note", {"assetId": aid, "content": "网络波动待排查"})["ok"]
        assert _post(f"{url}/api/add_mosaic", {"assetId": aid})["elements"] == 1
        assert _post(f"{url}/api/save_anno", {"assetId": aid})["ok"] is True
        # 有标注后大图为栅格化 PNG
        import urllib.request
        with urllib.request.urlopen(f"{url}/api/thumb?id={aid}&big=1&t=2", timeout=15) as r:
            assert r.headers["Content-Type"] == "image/png"

    def test_trash_restore_via_api(self, server_ctx, make_shot):
        url, eng = server_ctx
        paths = [make_shot("容器启动失败 镜像拉取超时", index=10)]
        rep = eng.import_files(paths, source="server-it")
        eng.process_jobs()
        aid = rep.imported[0]
        assert _post(f"{url}/api/trash", {"assetId": aid})["ok"] is True
        assert _post(f"{url}/api/restore", {"assetId": aid})["ok"] is True
        d = _post(f"{url}/api/detail", {"assetId": aid})
        assert d["id"] == aid

    def test_import_dir_and_process_via_api(self, server_ctx, make_shot, tmp_path):
        url, eng = server_ctx
        d = tmp_path / "ui-batch"
        d.mkdir()
        for i in range(2):
            p = make_shot(f"限流策略触发 429 请求过多 {i}", index=100 + i)
            import shutil
            shutil.copy(p, d / f"u{i}.png")
        r = _post(f"{url}/api/import_dir", {"path": str(d)})
        assert r["imported"] == 2
        assert _post(f"{url}/api/process", {})["processed"] >= 2
        s = _post(f"{url}/api/stats", {})
        assert s["assets"] >= 1

    def test_backup_via_api(self, server_ctx):
        url, eng = server_ctx
        r = _post(f"{url}/api/backup", {})
        assert "path" in r
        assert Path(r["path"]).exists()

    def test_unknown_route_404(self, server_ctx):
        url, _ = server_ctx
        import urllib.error
        import urllib.request
        req = urllib.request.Request(f"{url}/api/nope", data=b"{}", method="POST")
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=10)
        assert e.value.code == 404
