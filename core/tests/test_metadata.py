"""M5 元数据管理测试：标签 / 备注全文 / 自定义字段 / 回收站。"""
from __future__ import annotations

import pytest

from snapvault.metadata import MetadataService


@pytest.fixture()
def ms(db, config):
    return MetadataService(db, config)


@pytest.fixture()
def asset(db):
    return db.execute(
        "INSERT INTO assets(path, sha256) VALUES ('/a.png', 'h1') RETURNING id"
    ).fetchone()[0]


class TestTags:
    def test_create_and_list(self, ms):
        ms.create_tag("项目A/登录页", color="#ff0000")
        tags = ms.list_tags()
        assert len(tags) == 1 and tags[0].name == "项目A/登录页"
        assert tags[0].color == "#ff0000"

    def test_duplicate_rejected(self, ms):
        ms.create_tag("bug")
        with pytest.raises(ValueError, match="已存在"):
            ms.create_tag("bug")

    def test_rename(self, ms):
        ms.create_tag("旧标签")
        ms.rename_tag("旧标签", "新标签")
        assert [t.name for t in ms.list_tags()] == ["新标签"]

    def test_rename_missing(self, ms):
        with pytest.raises(ValueError, match="不存在"):
            ms.rename_tag("无", "有")

    def test_delete(self, ms, db, asset):
        ms.create_tag("待删")
        ms.set_asset_tags(asset, ["待删"])
        ms.delete_tag("待删")
        assert ms.list_tags() == []
        assert db.scalar("SELECT COUNT(*) FROM asset_tags WHERE asset_id=?", (asset,)) == 0

    def test_set_asset_tags_sync(self, ms, asset):
        ms.set_asset_tags(asset, ["a", "b", "a", ""])
        assert ms.get_asset_tags(asset) == ["a", "b"]
        ms.set_asset_tags(asset, ["c"])
        assert ms.get_asset_tags(asset) == ["c"]

    def test_tag_tree(self, ms):
        ms.create_tag("项目A/登录页")
        ms.create_tag("项目A/支付")
        ms.create_tag("运维")
        tree = ms.tag_tree()
        assert len(tree) == 2
        proj = next(n for n in tree if n["path"] == "项目A")
        assert len(proj["children"]) == 2
        assert {c["full"] for c in proj["children"]} == {"项目A/登录页", "项目A/支付"}
        assert proj["full"] is None  # 非叶子节点


class TestNotes:
    def test_set_and_get(self, ms, asset):
        ms.set_note(asset, "# 标题\nmarkdown 内容")
        assert ms.get_note(asset) == "# 标题\nmarkdown 内容"

    def test_update_and_delete(self, ms, asset):
        ms.set_note(asset, "v1")
        ms.set_note(asset, "v2")
        assert ms.get_note(asset) == "v2"
        ms.delete_note(asset)
        assert ms.get_note(asset) == ""

    def test_note_in_fts(self, ms, db, asset):
        ms.set_note(asset, "缺陷复现步骤见附件 编号 BUG-2026")
        rows = db.query(
            "SELECT asset_id, bm25(notes_fts) AS s FROM notes_fts "
            "WHERE notes_fts MATCH ? ORDER BY s", ("缺陷复现",))
        assert len(rows) == 1 and rows[0]["asset_id"] == asset
        ms.delete_note(asset)
        assert db.scalar("SELECT COUNT(*) FROM notes_fts") == 0


class TestCustomFields:
    def test_define_list(self, ms):
        fid = ms.define_field("defect_id", "缺陷ID", "text")
        fields = ms.list_fields()
        assert fields[0].id == fid
        assert fields[0].key == "defect_id" and fields[0].type == "text"

    def test_single_choice_options(self, ms):
        ms.define_field("env", "环境", "single_choice", ["dev", "staging", "prod"])
        f = ms.list_fields()[0]
        assert f.options == ["dev", "staging", "prod"]

    def test_invalid_type(self, ms):
        with pytest.raises(ValueError):
            ms.define_field("x", "X", "checkbox")

    def test_set_get_value(self, ms, asset):
        ms.define_field("version", "版本号")
        ms.set_field_value(asset, "version", "v1.2.3")
        assert ms.get_field_values(asset) == {"version": "v1.2.3"}
        ms.set_field_value(asset, "version", None)
        assert ms.get_field_values(asset) == {}

    def test_undefined_field(self, ms, asset):
        with pytest.raises(ValueError, match="未定义"):
            ms.set_field_value(asset, "nope", "1")

    def test_delete_field_clears_values(self, ms, asset):
        ms.define_field("tmp", "临时")
        ms.set_field_value(asset, "tmp", "x")
        fid = ms.list_fields()[0].id
        ms.delete_field(fid)
        assert ms.list_fields() == []
        assert ms.get_field_values(asset) == {}


class TestRecycleBin:
    def test_trash_restore(self, ms, db, asset):
        ms.trash(asset, reason="误删")
        row = ms.list_trashed()[0]
        assert row["id"] == asset and row["deleted_reason"] == "误删"
        ms.restore(asset)
        assert ms.list_trashed() == []
        assert db.scalar("SELECT deleted_at FROM assets WHERE id=?", (asset,)) is None

    def test_purge_removes_file(self, ms, config, db, make_image):
        from pathlib import Path

        from snapvault.importer import Importer

        img = make_image("purge 测试")
        aid = Importer(db, config).import_files([img]).imported[0]
        path = db.scalar("SELECT path FROM assets WHERE id=?", (aid,))
        assert Path(path).exists()
        ms.purge(aid)
        assert not Path(path).exists()
        assert db.scalar("SELECT COUNT(*) FROM assets WHERE id=?", (aid,)) == 0
        # 级联清理
        assert db.scalar("SELECT COUNT(*) FROM ocr_texts WHERE asset_id=?", (aid,)) == 0

    def test_cleanup_expired(self, ms, asset, db, config):
        import datetime as dt

        ms.trash(asset)
        db.execute(
            "UPDATE assets SET deleted_at=? WHERE id=?",
            ("2020-01-01T00:00:00Z", asset),  # 已过保留期
        )
        n = ms.cleanup_expired()
        assert n == 1
        assert db.scalar("SELECT COUNT(*) FROM assets") == 0

    def test_cleanup_keeps_recent(self, ms, asset, db):
        ms.trash(asset)
        assert ms.cleanup_expired() == 0
        assert db.scalar("SELECT COUNT(*) FROM assets") == 1

    def test_double_trash_keeps_first(self, ms, asset):
        ms.trash(asset, "a")
        ms.trash(asset, "b")
        rows = ms.list_trashed()
        assert len(rows) == 1 and rows[0]["deleted_reason"] == "a"
