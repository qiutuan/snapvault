"""SnapVault CLI：UI 未就绪时也可完成全部核心操作。

用法示例：
    snapvault init --data-dir ~/sv
    snapvault import ./screens --dir
    snapvault process
    snapvault search "支付成功" --k 10
    snapvault similar 3
    snapvault tag set 3 项目A/登录页
    snapvault note set 3 "第 3 次登录失败"
    snapvault field define defect_id 缺陷ID --type text
    snapvault field set 3 defect_id BUG-1024
    snapvault annotate save 3 ./anno.json
    snapvault export --include annotated --pdf --out ./out
    snapvault rebuild --force
    snapvault backup --passphrase secret
    snapvault backups
    snapvault restore ./backups/snapshot_xxx --passphrase secret
    snapvault export-library --out ./migration.zip
    snapvault import-library ./migration.zip --target ./new-root
    snapvault privacy-check --json
    snapvault trash 3 / restore 3 / purge 3
    snapvault verify-integrity
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .engine import SnapVault
from .exporter import ExportOptions
from .search import SearchFilter
from .util import human_size


def _json_dump(obj) -> None:
    print(json.dumps(obj, ensure_ascii=False, indent=2, default=str))


def _print_hits(hits) -> None:
    if not hits:
        print("（无结果）")
        return
    for h in hits:
        print(f"#{h.rank:>3}  asset_id={h.asset_id:<6} score={h.score:.4f} "
              f"sources={','.join(h.sources)}")
        if h.fragment:
            print(f"     片段: {h.fragment}")
        if h.snippet:
            print(f"     预览: {h.snippet[:90].replace(chr(10), ' / ')}")


# ----------------------------------------------------------------------
# 子命令实现
# ----------------------------------------------------------------------
def cmd_init(args) -> int:
    from .config import Config
    from .privacy import startup_network_check

    cfg = Config(args.data_dir)
    engine = SnapVault(args.data_dir, setup_logs=True)
    report = startup_network_check()
    print(f"数据根目录: {cfg.root}")
    print(f"数据库: {cfg.db_path()}")
    print(f"网络探测自检: {'通过' if report.passed else '未通过'}")
    engine.close()
    return 0


def cmd_import(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        report = engine.import_directory(args.paths[0], source=args.source) \
            if args.dir else engine.import_files(args.paths, source=args.source)
        print(f"导入完成: 新增 {len(report.imported)}，去重 {len(report.duplicates)}，"
              f"恢复 {len(report.restored)}，跳过 {len(report.skipped_ext)}，"
              f"失败 {len(report.failed)}")
        if args.process:
            n = engine.process_jobs()
            print(f"已处理任务 {n}")
        for p, err in report.failed:
            print(f"  [失败] {p}: {err}", file=sys.stderr)
    finally:
        engine.close()
    return 0


def cmd_process(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        n = engine.process_jobs(max_runs=args.max_runs)
        print(f"处理任务 {n} 个；剩余: {engine.job_stats()}")
    finally:
        engine.close()
    return 0


def cmd_search(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        f = SearchFilter()
        if args.tag:
            f.tags = [args.tag]
        if args.since or args.until:
            f.created_range = (args.since, args.until)
        if args.min_conf is not None:
            f.min_confidence = args.min_conf
        if args.size:
            wh = args.size.lower().split("x")
            f.min_width = int(wh[0]); f.min_height = int(wh[1]) if len(wh) > 1 else 0
        hits = engine.search(args.query, k=args.k, filters=f)
        if args.json:
            _json_dump([h.__dict__ for h in hits])
        else:
            print(f"命中 {len(hits)} 条（Top-{args.k}）:")
            _print_hits(hits)
    finally:
        engine.close()
    return 0


def cmd_similar(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        hits = engine.similar(args.asset_id, k=args.k)
        if args.json:
            _json_dump([h.__dict__ for h in hits])
        else:
            print(f"与 #{args.asset_id} 最相似:")
            _print_hits(hits)
    finally:
        engine.close()
    return 0


def cmd_stats(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        s = engine.stats()
        s["db_size_human"] = human_size(s.pop("db_size"))
        if args.json:
            _json_dump(s)
        else:
            for k, v in s.items():
                print(f"{k}: {v}")
    finally:
        engine.close()
    return 0


def _tag_args(p):
    sub = p.add_subparsers(dest="tag_cmd", required=True)
    sp = sub.add_parser("list")
    sp.add_argument("--json", action="store_true")
    sp2 = sub.add_parser("set")
    sp2.add_argument("asset_id", type=int)
    sp2.add_argument("tags", nargs="+")
    sp3 = sub.add_parser("rm")
    sp3.add_argument("tag", type=str)
    sp3.add_argument("--rename-to", type=str, default=None)


def cmd_tag(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        m = engine.metadata
        if args.tag_cmd == "list":
            out = [{"name": t.name, "color": t.color} for t in m.list_tags()]
            if args.json:
                _json_dump(out)
            else:
                for t in m.list_tags():
                    print(f"{t.name}\t{t.color or '-'}")
        elif args.tag_cmd == "set":
            applied = m.set_asset_tags(args.asset_id, args.tags)
            print(f"资产 #{args.asset_id} 标签: {', '.join(applied)}")
        elif args.tag_cmd == "rm":
            if args.rename_to:
                m.rename_tag(args.tag, args.rename_to)
                print(f"标签 {args.tag} → {args.rename_to}")
            else:
                m.delete_tag(args.tag)
                print(f"已删除标签 {args.tag}")
    finally:
        engine.close()
    return 0


def _note_args(p):
    sub = p.add_subparsers(dest="note_cmd", required=True)
    sp = sub.add_parser("get")
    sp.add_argument("asset_id", type=int)
    sp2 = sub.add_parser("set")
    sp2.add_argument("asset_id", type=int)
    sp2.add_argument("content", type=str)
    sp3 = sub.add_parser("rm")
    sp3.add_argument("asset_id", type=int)


def cmd_note(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        m = engine.metadata
        if args.note_cmd == "get":
            print(m.get_note(args.asset_id) or "（无备注）")
        elif args.note_cmd == "set":
            m.set_note(args.asset_id, args.content)
            print(f"已写入备注 #{args.asset_id}")
        elif args.note_cmd == "rm":
            m.delete_note(args.asset_id)
            print(f"已删除备注 #{args.asset_id}")
    finally:
        engine.close()
    return 0


def _field_args(p):
    sub = p.add_subparsers(dest="field_cmd", required=True)
    sp = sub.add_parser("list")
    sp.add_argument("--json", action="store_true")
    sp2 = sub.add_parser("define")
    sp2.add_argument("key")
    sp2.add_argument("label")
    sp2.add_argument("--type", default="text",
                     choices=["text", "single_choice", "date"])
    sp3 = sub.add_parser("set")
    sp3.add_argument("asset_id", type=int)
    sp3.add_argument("key")
    sp3.add_argument("value")
    sp4 = sub.add_parser("get")
    sp4.add_argument("asset_id", type=int)


def cmd_field(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        m = engine.metadata
        if args.field_cmd == "list":
            out = [{"key": f.key, "label": f.label, "type": f.ftype}
                   for f in m.list_fields()]
            _json_dump(out) if args.json else print(
                "\n".join(f"{f.key}\t{f.label}\t{f.ftype}" for f in m.list_fields()))
        elif args.field_cmd == "define":
            m.define_field(args.key, args.label, ftype=args.type)
            print(f"已定义字段 {args.key} ({args.label}, {args.type})")
        elif args.field_cmd == "set":
            m.set_field_value(args.asset_id, args.key, args.value)
            print(f"已写入 {args.key}={args.value} → #{args.asset_id}")
        elif args.field_cmd == "get":
            _json_dump(m.get_field_values(args.asset_id))
    finally:
        engine.close()
    return 0


def _annotate_args(p):
    sub = p.add_subparsers(dest="anno_cmd", required=True)
    sp = sub.add_parser("get")
    sp.add_argument("asset_id", type=int)
    sp.add_argument("--out", type=str, default=None)
    sp2 = sub.add_parser("save")
    sp2.add_argument("asset_id", type=int)
    sp2.add_argument("json_file", type=str)
    sp3 = sub.add_parser("raster")
    sp3.add_argument("asset_id", type=int)
    sp3.add_argument("--out", type=str, required=True)


def cmd_annotate(args) -> int:
    from .annotations import AnnotationModel, rasterize

    engine = SnapVault(args.data_dir)
    try:
        if args.anno_cmd == "get":
            model = engine.annotations.get_model(args.asset_id)
            out = model.to_json(indent=2)
            if args.out:
                Path(args.out).write_text(out, encoding="utf-8")
                print(f"已写出标注 JSON → {args.out}")
            else:
                print(out)
        elif args.anno_cmd == "save":
            raw = Path(args.json_file).read_text(encoding="utf-8")
            model = AnnotationModel.from_json(raw)
            engine.annotations.save_model(args.asset_id, model)
            print(f"已保存 {len(model.elements)} 个图元 → #{args.asset_id}")
        elif args.anno_cmd == "raster":
            row = engine.db.query_one("SELECT path, width, height FROM assets WHERE id=?",
                                      (args.asset_id,))
            model = engine.annotations.get_model(args.asset_id)
            if not model.elements:
                print("该资产无标注，输出原图", file=sys.stderr)
            img = rasterize(row["path"], model)
            img.save(args.out)
            print(f"已栅格化 → {args.out}")
    finally:
        engine.close()
    return 0


def cmd_export(args) -> int:
    import shutil

    engine = SnapVault(args.data_dir)
    try:
        f = SearchFilter()
        if args.tag:
            f.tags = [args.tag]
        opts = ExportOptions(
            include=args.include,
            with_pdf=args.pdf,
            with_csv=not args.no_csv,
            filters=f,
        )
        report = engine.export(opts)
        dest = Path(args.out) if args.out else Path(report.package_dir)
        if args.out:
            dest.mkdir(parents=True, exist_ok=True)
            for item in Path(report.package_dir).iterdir():
                shutil.move(str(item), str(dest / item.name))
        print(f"导出完成: {dest}")
        print(f"  图片 {report.asset_count} 张, CSV {report.csv_path or '—'}, "
              f"PDF {report.pdf_path or '—'}")
        for p, err in report.errors:
            print(f"  [隔离] {p}: {err}", file=sys.stderr)
    finally:
        engine.close()
    return 0


def cmd_rebuild(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        rep = engine.rebuild_indexes(ocr=args.ocr, embed=args.embed,
                                     thumbs=args.thumbs, force=args.force)
        print(f"重建完成: 总数 {rep.total}, OCR {rep.ocr_done} (跳过 {rep.ocr_skipped}), "
              f"向量 {rep.embed_done}, 缩略图 {rep.thumbs_done}, 失败 {len(rep.failed)}")
        for aid, err in rep.failed:
            print(f"  [失败] #{aid}: {err}", file=sys.stderr)
    finally:
        engine.close()
    return 0


def cmd_backup(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        info = engine.backup.create_snapshot(encrypt_passphrase=args.passphrase)
        print(f"快照已创建: {info.path}")
        print(f"  资产 {info.asset_count}, {human_size(info.size_bytes)}, "
              f"加密={info.encrypted}")
    finally:
        engine.close()
    return 0


def cmd_backups(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        snaps = engine.backup.list_snapshots()
        for s in snaps:
            print(f"{s.created_at}  {s.asset_count:>6} assets  "
                  f"{human_size(s.size_bytes):>10}  加密={s.encrypted}  {s.path}")
    finally:
        engine.close()
    return 0


def cmd_restore(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        n = engine.backup.restore_snapshot(args.snapshot, args.passphrase)
        print(f"已恢复 {n} 个资产")
    finally:
        engine.close()
    return 0


def cmd_export_library(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        z = engine.export_library(dst=args.out)
        print(f"库导出包: {z}")
    finally:
        engine.close()
    return 0


def cmd_import_library(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        target = engine.import_library(args.zip, target_root=args.target)
        print(f"库导入完成 → {target}")
    finally:
        engine.close()
    return 0


def cmd_privacy(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        rep = engine.privacy_check()
        if args.json:
            _json_dump(rep.to_dict())
        else:
            print(f"隐私自检: {'通过' if rep.passed else '未通过'}")
            for c in rep.checks:
                print(f"  - {c['name']}: {'PASS' if c['passed'] else 'FAIL'} {c}")
    finally:
        engine.close()
    return 0


def _trash_args(p):
    sub = p.add_subparsers(dest="trash_cmd", required=True)
    sp = sub.add_parser("move")
    sp.add_argument("asset_id", type=int)
    sp2 = sub.add_parser("restore")
    sp2.add_argument("asset_id", type=int)
    sp3 = sub.add_parser("purge")
    sp3.add_argument("asset_id", type=int)
    sp4 = sub.add_parser("list")
    sp4.add_argument("--json", action="store_true")


def cmd_trash(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        m = engine.metadata
        if args.trash_cmd == "move":
            m.trash(args.asset_id)
            print(f"已移入回收站 #{args.asset_id}")
        elif args.trash_cmd == "restore":
            m.restore(args.asset_id)
            print(f"已恢复 #{args.asset_id}")
        elif args.trash_cmd == "purge":
            m.purge(args.asset_id)
            print(f"已彻底删除 #{args.asset_id}")
        elif args.trash_cmd == "list":
            rows = m.list_trashed()
            if args.json:
                _json_dump(rows)
            else:
                for r in rows:
                    print(f"#{r['id']}  {r['path']}  删除于 {r['deleted_at']}")
    finally:
        engine.close()
    return 0


def cmd_verify(args) -> int:
    engine = SnapVault(args.data_dir)
    try:
        ok = engine.db.integrity_ok()
        print(f"数据库完整性: {'ok' if ok else 'FAILED'}")
        return 0 if ok else 1
    finally:
        engine.close()


def cmd_screenshot(args) -> int:
    from .screenshot import capture

    engine = SnapVault(args.data_dir)
    try:
        region = tuple(int(v) for v in args.region.split(",")) if args.region else None
        shot = capture(engine.config.images_dir(), monitor=args.monitor,
                       region=region, source=args.source)
        print(f"已截图: {shot.path} ({shot.width}x{shot.height})")
        if args.import_:
            rep = engine.import_files([shot.path], source=args.source)
            print(f"已入库: {len(rep.imported)} 张")
    finally:
        engine.close()
    return 0


def cmd_serve(args) -> int:
    from .server import serve

    httpd = serve(port=args.port, data_dir=args.data_dir)
    print(f"SnapVault 本地服务: http://127.0.0.1:{args.port}  （仅本机，Ctrl+C 退出）")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    return 0


def cmd_version(args) -> int:
    from . import __version__

    print(f"snapvault-core {__version__}")
    return 0


# ----------------------------------------------------------------------
# 解析器
# ----------------------------------------------------------------------
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="snapvault", description="SnapVault 本地截图资产管理系统 CLI")
    p.add_argument("--data-dir", default=None,
                   help="数据根目录（默认 $SNAPVAULT_DATA_DIR 或 ~/SnapVault）")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("init", help="初始化数据目录并做网络探测自检")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("import", help="导入图片或目录")
    sp.add_argument("paths", nargs="+")
    sp.add_argument("--dir", action="store_true", help="整目录增量导入")
    sp.add_argument("--source", default="import")
    sp.add_argument("--process", action="store_true", help="导入后立即跑任务队列")
    sp.set_defaults(func=cmd_import)

    sp = sub.add_parser("process", help="处理异步任务队列(OCR/embedding/缩略图)")
    sp.add_argument("--max-runs", type=int, default=None)
    sp.set_defaults(func=cmd_process)

    sp = sub.add_parser("search", help="混合搜索")
    sp.add_argument("query")
    sp.add_argument("--k", type=int, default=20)
    sp.add_argument("--tag", default=None)
    sp.add_argument("--since", default=None)
    sp.add_argument("--until", default=None)
    sp.add_argument("--min-conf", type=float, default=None)
    sp.add_argument("--size", default=None, help="如 800x600")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_search)

    sp = sub.add_parser("similar", help="以图搜图")
    sp.add_argument("asset_id", type=int)
    sp.add_argument("--k", type=int, default=10)
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_similar)

    sp = sub.add_parser("stats", help="统计")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_stats)

    sp = sub.add_parser("tag", help="标签管理")
    _tag_args(sp)
    sp.set_defaults(func=cmd_tag)

    sp = sub.add_parser("note", help="备注管理")
    _note_args(sp)
    sp.set_defaults(func=cmd_note)

    sp = sub.add_parser("field", help="自定义字段")
    _field_args(sp)
    sp.set_defaults(func=cmd_field)

    sp = sub.add_parser("annotate", help="标注管理")
    _annotate_args(sp)
    sp.set_defaults(func=cmd_annotate)

    sp = sub.add_parser("export", help="批量导出")
    sp.add_argument("--include", choices=["original", "annotated"], default="annotated")
    sp.add_argument("--pdf", action="store_true")
    sp.add_argument("--no-csv", action="store_true")
    sp.add_argument("--tag", default=None)
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_export)

    sp = sub.add_parser("rebuild", help="从 db+图片离线重建索引")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--ocr", dest="ocr", action="store_true", default=True)
    sp.add_argument("--no-ocr", dest="ocr", action="store_false")
    sp.add_argument("--embed", dest="embed", action="store_true", default=True)
    sp.add_argument("--no-embed", dest="embed", action="store_false")
    sp.add_argument("--thumbs", dest="thumbs", action="store_true", default=True)
    sp.add_argument("--no-thumbs", dest="thumbs", action="store_false")
    sp.set_defaults(func=cmd_rebuild)

    sp = sub.add_parser("backup", help="创建快照")
    sp.add_argument("--passphrase", default=None)
    sp.set_defaults(func=cmd_backup)

    sp = sub.add_parser("backups", help="列出快照")
    sp.set_defaults(func=cmd_backups)

    sp = sub.add_parser("restore", help="从快照恢复")
    sp.add_argument("snapshot")
    sp.add_argument("--passphrase", default=None)
    sp.set_defaults(func=cmd_restore)

    sp = sub.add_parser("export-library", help="整库导出(迁移包)")
    sp.add_argument("--out", default=None)
    sp.set_defaults(func=cmd_export_library)

    sp = sub.add_parser("import-library", help="导入迁移包")
    sp.add_argument("zip")
    sp.add_argument("--target", default=None)
    sp.set_defaults(func=cmd_import_library)

    sp = sub.add_parser("privacy-check", help="隐私自检")
    sp.add_argument("--json", action="store_true")
    sp.set_defaults(func=cmd_privacy)

    sp = sub.add_parser("trash", help="回收站")
    _trash_args(sp)
    sp.set_defaults(func=cmd_trash)

    sp = sub.add_parser("verify-integrity", help="数据库完整性校验")
    sp.set_defaults(func=cmd_verify)

    sp = sub.add_parser("screenshot", help="屏幕截图（monitor=0 表示所有屏幕）")
    sp.add_argument("--monitor", type=int, default=0)
    sp.add_argument("--region", default=None, help="X,Y,W,H")
    sp.add_argument("--source", default="screen")
    sp.add_argument("--import", dest="import_", action="store_true", help="截图后直接入库")
    sp.set_defaults(func=cmd_screenshot)

    sp = sub.add_parser("serve", help="启动本地 Web 演示服务（仅 127.0.0.1）")
    sp.add_argument("--port", type=int, default=8765)
    sp.set_defaults(func=cmd_serve)

    sp = sub.add_parser("version", help="版本")
    sp.set_defaults(func=cmd_version)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        return args.func(args) or 0
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130
    except Exception as exc:  # noqa: BLE001
        print(f"错误: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
