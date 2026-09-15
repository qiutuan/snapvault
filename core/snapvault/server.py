"""本地 HTTP 服务（Web 演示 / Tauri 兜底通道）：只监听 127.0.0.1，零外网。

同时托管前端静态文件（app/dist），实现「浏览器直接演示完整闭环」，
以及为 Tauri 壳提供 invoke 之外的备选 API。
"""
from __future__ import annotations

import base64
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .engine import SnapVault
from .exporter import ExportOptions

REPO = Path(__file__).resolve().parents[2]
STATIC_CANDIDATES = [REPO / "app" / "dist", REPO / "app" / "src"]


def _raster_bytes(path, model) -> bytes:
    from io import BytesIO

    from .annotations import rasterize

    bio = BytesIO()
    rasterize(str(path), model, font_path=None).save(bio, "PNG")
    return bio.getvalue()


class _Handler(BaseHTTPRequestHandler):
    server_version = "SnapVault/0.1"
    engine: SnapVault | None = None

    # ------------------------------------------------------------------
    def log_message(self, fmt, *args):  # 静默访问日志（避免刷屏）
        pass

    # ------------------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json") -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj) -> None:
        self._send(200, json.dumps(obj, ensure_ascii=False, default=str).encode("utf-8"))

    def _body(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length", 0))
            return json.loads(self.rfile.read(n) or b"{}") if n else {}
        except (ValueError, json.JSONDecodeError):
            return {}

    # ------------------------------------------------------------------
    def do_GET(self):
        if self.path.startswith("/api/thumb"):
            self._thumb()
            return
        if self.path == "/api/health":
            self._json({"ok": True})
            return
        self._static()

    def do_POST(self):
        eng = _Handler.engine
        body = self._body()
        path = self.path.rstrip("/")
        try:
            if path == "/api/init":
                eng = _ensure_engine(body.get("dataDir"))
                cfg = eng.config
                cfg.set_many({"naming_template": cfg.naming_template})
                self._json({"initialized": True, "dataDir": str(cfg.root)})
            elif path == "/api/get_config":
                eng = _Handler.engine
                cfg = eng.config
                self._json({"initialized": True, "dataDir": str(cfg.root),
                            "hkFull": cfg.get("hotkeys", {}).get("fullscreen"),
                            "hkRegion": cfg.get("hotkeys", {}).get("region"),
                            "ocr": cfg.ocr_enabled, "backupKeep": cfg.backup_keep})
            elif path == "/api/search":
                f = _filter(body)
                hits = eng.search(body.get("query", ""), k=int(body.get("k", 200)), filters=f)
                self._json([h.__dict__ for h in hits])
            elif path == "/api/detail":
                aid = int(body["assetId"])
                row = eng.db.query_one("SELECT * FROM assets WHERE id=?", (aid,))
                if not row:
                    self._json(None)
                    return
                chunks = eng.db.query(
                    "SELECT text FROM ocr_texts WHERE asset_id=? ORDER BY chunk_index",
                    (aid,))
                self._json({
                    "id": aid, "path": row["path"], "tags": eng.metadata.get_asset_tags(aid),
                    "note": eng.metadata.get_note(aid),
                    "ocr": "\n".join(c["text"] for c in chunks),
                    "fields": eng.metadata.get_field_values(aid),
                })
            elif path == "/api/tags":
                self._json([{"name": t.name, "color": t.color}
                            for t in eng.metadata.list_tags()])
            elif path == "/api/stats":
                s = eng.stats()
                s["db_size"] = eng.config.db_path().stat().st_size if \
                    eng.config.db_path().exists() else 0
                self._json(s)
            elif path == "/api/import_dir":
                rep = eng.import_directory(body.get("path", ""), source="ui")
                self._json({"imported": len(rep.imported), "duplicates": len(rep.duplicates),
                            "failed": rep.failed[:10]})
            elif path == "/api/process":
                n = eng.process_jobs()
                self._json({"processed": n})
            elif path == "/api/export":
                r = eng.export(ExportOptions(
                    include=body.get("include", "annotated"),
                    with_pdf=bool(body.get("pdf", True)), with_csv=True))
                self._json({"package_dir": r.package_dir, "csv": r.csv_path,
                            "pdf": r.pdf_path, "errors": r.errors[:10]})
            elif path == "/api/trash":
                eng.metadata.trash(int(body["assetId"]))
                self._json({"ok": True})
            elif path == "/api/restore":
                eng.metadata.restore(int(body["assetId"]))
                self._json({"ok": True})
            elif path == "/api/save_note":
                eng.metadata.set_note(int(body["assetId"]), body.get("content", ""))
                self._json({"ok": True})
            elif path == "/api/add_mosaic":
                aid = int(body["assetId"])
                row = eng.db.query_one("SELECT width, height FROM assets WHERE id=?", (aid,))
                from .annotations import AnnotationModel, Element

                model = eng.annotations.get_model(aid, image_size=(row["width"], row["height"]))
                model.add(Element("mosaic", rect=(int(row["width"]*0.1), int(row["height"]*0.1),
                                                  int(row["width"]*0.5), int(row["height"]*0.4)),
                                  strength=0.9))
                eng.annotations.save_model(aid, model)
                self._json({"ok": True, "elements": len(model.elements)})
            elif path == "/api/save_anno":
                aid = int(body["assetId"])
                model = eng.annotations.get_model(aid)
                eng.annotations.save_model(aid, model)
                self._json({"ok": True})
            elif path == "/api/backup":
                info = eng.backup.create_snapshot(encrypt_passphrase=body.get("passphrase"))
                self._json({"path": info.path, "asset_count": info.asset_count})
            elif path == "/api/settings":
                cfg = eng.config
                cfg.set("ocr_enabled", bool(body.get("ocr", True)))
                cfg.set("backup_keep", int(body.get("backupKeep", 7)))
                self._json({"ok": True})
            else:
                self._send(404, b'{"error":"not found"}')
        except Exception as exc:  # noqa: BLE001
            self._send(500, json.dumps({"error": str(exc)}).encode("utf-8"))

    # ------------------------------------------------------------------
    def _thumb(self):
        from urllib.parse import parse_qs, urlparse

        qs = parse_qs(urlparse(self.path).query)
        eng = _Handler.engine
        aid = int(qs.get("id", ["0"])[0])
        big = qs.get("big", ["0"])[0] == "1"
        row = eng.db.query_one("SELECT path FROM assets WHERE id=?", (aid,))
        if not row:
            self._send(404, b"")
            return
        if big:
            # 大图：若存在标注则直接返回栅格化预览（打码可视化）
            try:
                model = eng.annotations.get_model(aid)
                if model.elements:
                    data = _raster_bytes(Path(row["path"]), model)
                    self._send(200, data, "image/png")
                    return
            except Exception:  # noqa: BLE001 标注渲染失败回退原图
                pass
            p = Path(row["path"])
        else:
            padded = eng.config.thumbnails_dir() / f"{aid:06d}.jpg"
            plain = eng.config.thumbnails_dir() / f"{aid}.jpg"
            p = padded if padded.exists() else plain
        if not p.exists() or p.stat().st_size == 0:
            self._send(404, b"")
            return
        ctype = "image/jpeg" if p.suffix == ".jpg" else "image/png"
        self._send(200, p.read_bytes(), ctype)

    def _static(self):
        for base in STATIC_CANDIDATES:
            rel = self.path.split("?", 1)[0].lstrip("/")
            target = base / rel if rel else base / "index.html"
            if not str(target).startswith(str(base)):
                self._send(403, b"")
                return
            if target.is_file():
                ctype = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
                         ".css": "text/css", ".png": "image/png", ".svg": "image/svg+xml",
                         ".woff2": "font/woff2"}.get(target.suffix, "application/octet-stream")
                self._send(200, target.read_bytes(), ctype)
                return
        self._send(404, b"not found")


def _ensure_engine(data_dir: str | None) -> SnapVault:
    if _Handler.engine is None:
        _Handler.engine = SnapVault(data_dir, acquire_lock=False)
    return _Handler.engine


def _filter(body: dict):
    from .search import SearchFilter

    f = SearchFilter()
    if body.get("tag"):
        f.tags = [body["tag"]]
    if body.get("trashed"):
        f.include_trashed = True
    return f


def serve(host: str = "127.0.0.1", port: int = 8765, data_dir: str | None = None,
          static: Path | None = None) -> ThreadingHTTPServer:
    global STATIC_CANDIDATES
    if static is not None:
        STATIC_CANDIDATES = [static]
    _ensure_engine(data_dir)
    httpd = ThreadingHTTPServer((host, port), _Handler)
    return httpd
