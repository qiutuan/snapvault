"""M4 检索测试：RRF 融合 / FTS 关键词 / 组合过滤 / 以图搜图 / 片段高亮。"""
from __future__ import annotations

import hashlib

import numpy as np
import pytest

from snapvault.rrf import RRF
from snapvault.search import HybridSearch, SearchFilter, fts_query_escape


class FakeEmbedder:
    """确定性伪嵌入：同文本同向量，用于隔离测试融合逻辑（不依赖真实模型）。"""

    model_name = "fake"
    dim = 512

    def __init__(self, dim: int = 512):
        self.dim = dim

    def is_available(self) -> bool:
        return True

    def embed(self, texts: list[str]) -> np.ndarray:
        out = []
        for t in texts:
            seed = int(hashlib.md5(t.encode("utf-8")).hexdigest()[:8], 16)
            rng = np.random.default_rng(seed)
            out.append(rng.standard_normal(self.dim).astype(np.float32))
        return np.asarray(out, dtype=np.float32)

    def embed_one(self, text: str) -> np.ndarray:
        return self.embed([text])[0]


def _add_asset(db, text: str, tags: list[str] | None = None, created: str | None = None,
               width: int = 640, height: int = 360, conf: float = 0.9,
               custom: dict[str, str] | None = None) -> int:
    with db.tx() as conn:
        cur = conn.execute(
            "INSERT INTO assets(path, sha256, width, height, ocr_confidence, "
            "ocr_status, created_at) VALUES (?, ?, ?, ?, ?, 'done', ?) RETURNING id",
            (f"/img_{abs(hash(text))}.png", hashlib.md5(text.encode()).hexdigest(),
             width, height, conf, created or "2026-09-01T08:00:00Z"),
        )
        aid = cur.fetchone()[0]
        conn.execute("INSERT INTO ocr_texts(asset_id, chunk_index, text, confidence) "
                     "VALUES (?, 0, ?, ?)", (aid, text, conf))
        conn.execute("INSERT INTO ocr_fts(asset_id, chunk_index, text) VALUES (?, 0, ?)",
                     (aid, text))
        for tag in tags or []:
            conn.execute("INSERT OR IGNORE INTO tags(name) VALUES (?)", (tag,))
            tid = conn.execute("SELECT id FROM tags WHERE name=?", (tag,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO asset_tags(asset_id, tag_id) VALUES (?, ?)",
                         (aid, tid))
        for key, value in (custom or {}).items():
            conn.execute("INSERT OR IGNORE INTO custom_field_defs(key, label, type) "
                         "VALUES (?, ?, 'text')", (key, key))
            fid = conn.execute("SELECT id FROM custom_field_defs WHERE key=?",
                               (key,)).fetchone()[0]
            conn.execute("INSERT OR IGNORE INTO custom_field_values(asset_id, field_id, value) "
                         "VALUES (?, ?, ?)", (aid, fid, value))
    return aid


@pytest.fixture()
def seeded(db):
    """构造带已知文字与标签的检索样本库。"""
    ids = {}
    ids["login"] = _add_asset(db, "登录页面加载失败 服务器返回500",
                              tags=["项目A/登录页"], conf=0.95, width=1280)
    ids["pay"] = _add_asset(db, "订单支付成功 金额128元 订单号20260915001",
                            tags=["项目A/支付"], created="2026-09-10T10:00:00Z",
                            custom={"defect_id": "BUG-100"})
    ids["redis"] = _add_asset(db, "Timeout connecting to Redis cluster at 10.0.0.1",
                              tags=["运维"], conf=0.6)
    ids["empty"] = _add_asset(db, "No text here just numbers 12345", tags=["其他"])
    return ids


class TestRRF:
    def test_fusion(self):
        rrf = RRF(k=60)
        rrf.add_ranking("fts", [1, 2, 3])
        rrf.add_ranking("vec", [3, 1, 4])
        fused = rrf.fuse()
        assert fused[0][0] == 1  # 两个来源都命中且排名高
        assert {aid for aid, _ in fused} == {1, 2, 3, 4}

    def test_sources(self):
        rrf = RRF()
        rrf.add_ranking("fts", [1])
        rrf.add_ranking("vec", [1, 2])
        assert sorted(rrf.sources_of(1)) == ["fts", "vec"]
        assert rrf.sources_of(2) == ["vec"]

    def test_limit(self):
        rrf = RRF()
        rrf.add_ranking("a", [1, 2, 3])
        assert len(rrf.fuse(limit=2)) == 2


class TestFtsQueryEscape:
    def test_escape_quotes(self):
        assert fts_query_escape('say "hi" now') == '"say" """hi""" "now"'

    def test_empty(self):
        assert fts_query_escape("   ") == ""


class TestKeywordSearch:
    def test_find_by_chinese(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("登录页面")
        assert hits and hits[0].asset_id == seeded["login"]

    def test_find_by_english(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("Redis")
        assert hits and hits[0].asset_id == seeded["redis"]

    def test_fragment_highlighted(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("订单支付")
        assert "<mark>订单支付</mark>" in hits[0].fragment

    def test_short_query_fallback(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("支付")  # 2 字符 → LIKE 回退
        assert hits and hits[0].asset_id == seeded["pay"]

    def test_no_result(self, db, seeded):
        hs = HybridSearch(db, None)
        assert hs.search("不存在的关键词xyz") == []


class TestHybridFusion:
    def test_fusion_combines_sources(self, db, seeded):
        hs = HybridSearch(db, FakeEmbedder())
        hits = hs.search("登录页面")
        # 融合后登录页截图仍第一，且可能携带 vec 来源
        assert hits[0].asset_id == seeded["login"]

    def test_degraded_without_embedder(self, db, seeded):
        hs = HybridSearch(db, None)  # 无 embedding 引擎 → 纯关键词降级
        hits = hs.search("订单支付成功")
        assert hits and hits[0].asset_id == seeded["pay"]


class TestFilters:
    def test_tag_filter(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(tags=["项目A"]))
        ids = {h.asset_id for h in hits}
        assert seeded["login"] in ids and seeded["pay"] in ids
        assert seeded["redis"] not in ids

    def test_tag_hierarchy_prefix(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(tags=["项目A"]))  # 前缀命中 项目A/登录页
        assert seeded["login"] in {h.asset_id for h in hits}

    def test_tag_any(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(tags=["运维", "其他"], tag_any=True))
        ids = {h.asset_id for h in hits}
        assert seeded["redis"] in ids and seeded["empty"] in ids

    def test_date_range(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(date_from="2026-09-10", date_to="2026-09-30"))
        assert seeded["pay"] in {h.asset_id for h in hits}
        assert seeded["login"] not in {h.asset_id for h in hits}

    def test_custom_field(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(custom_fields={"defect_id": "BUG-100"}))
        assert seeded["pay"] in {h.asset_id for h in hits}

    def test_size_filter(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(min_width=1000))
        assert seeded["login"] in {h.asset_id for h in hits}
        assert seeded["pay"] not in {h.asset_id for h in hits}

    def test_confidence_range(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(min_confidence=0.9))
        ids = {h.asset_id for h in hits}
        assert seeded["login"] in ids and seeded["pay"] in ids
        assert seeded["redis"] not in ids  # conf=0.6

    def test_exclude_trashed(self, db, seeded):
        db.execute("UPDATE assets SET deleted_at='2026-09-02T00:00:00Z' WHERE id=?",
                   (seeded["pay"],))
        hs = HybridSearch(db, None)
        hits = hs.search("订单支付")
        assert all(h.asset_id != seeded["pay"] for h in hits)
        hits2 = hs.search("订单支付", filters=SearchFilter(include_trashed=True))
        assert seeded["pay"] in {h.asset_id for h in hits2}

    def test_combined_filters(self, db, seeded):
        hs = HybridSearch(db, None)
        hits = hs.search("", filters=SearchFilter(
            tags=["项目A"], custom_fields={"defect_id": "BUG-100"}))
        assert [h.asset_id for h in hits] == [seeded["pay"]]


class TestSimilarSearch:
    def test_find_similar(self, db, seeded):
        hs = HybridSearch(db, FakeEmbedder())
        # 给"支付"资产建真实向量
        with db.tx() as conn:
            vec = FakeEmbedder().embed_one("订单支付成功 金额128元 订单号20260915001")
            blob = vec.astype("<f4").tobytes()
            conn.execute("INSERT INTO embeddings(asset_id, embedding, model, dim) "
                         "VALUES (?, ?, 'fake', 512)", (seeded["pay"], blob))
            conn.execute("INSERT INTO vec_embeddings(rowid, embedding) VALUES (?, ?)",
                         (seeded["pay"], blob))
            vec2 = FakeEmbedder().embed_one("Timeout connecting to Redis cluster at 10.0.0.1")
            blob2 = vec2.astype("<f4").tobytes()
            conn.execute("INSERT INTO embeddings(asset_id, embedding, model, dim) "
                         "VALUES (?, ?, 'fake', 512)", (seeded["redis"], blob2))
            conn.execute("INSERT INTO vec_embeddings(rowid, embedding) VALUES (?, ?)",
                         (seeded["redis"], blob2))
        hits = hs.similar(seeded["pay"], k=5)
        assert len(hits) >= 1
        assert all(h.asset_id != seeded["pay"] for h in hits)

    def test_similar_no_embedder(self, db, seeded):
        hs = HybridSearch(db, None)
        assert hs.similar(seeded["pay"]) == []

    def test_similar_missing_vector(self, db, seeded):
        hs = HybridSearch(db, FakeEmbedder())
        assert hs.similar(seeded["empty"]) == []
