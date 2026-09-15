"""混合检索（M4，产品核心）：

1. FTS5 关键词匹配（ocr_fts + 可选 notes_fts，trigram 分词）
2. 向量语义相似度（sqlite-vec，bge-small-zh-v1.5）
3. RRF 融合排序
4. 组合过滤：标签（层级前缀匹配）/ 时间范围 / 自定义字段 / 图片尺寸 / OCR 置信度
5. 命中片段高亮（OCR 文本命中窗口）
6. 相似图片搜索（同向量空间）
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from .db import Database
from .embedding_engine import EmbeddingEngine
from .rrf import RRF

# bge 检索指令（仅查询侧，文档侧不追加——BAAI/bge 官方建议）
QUERY_INSTRUCTION = "为这个句子生成表示以用于检索相关文章："

_FTS_LIMIT = 200
_VEC_LIMIT = 200
_RRF_K = 60
_SHORT_QUERY = 3  # trigram 最短匹配长度


@dataclass
class SearchFilter:
    tags: list[str] | None = None          # 层级前缀匹配
    tag_any: bool = False                  # True=任一命中；False=全部命中
    date_from: str | None = None           # 'YYYY-MM-DD'
    date_to: str | None = None
    custom_fields: dict[str, str] | None = None   # {field_key: value}
    min_width: int | None = None
    min_height: int | None = None
    max_width: int | None = None
    max_height: int | None = None
    min_confidence: float | None = None
    max_confidence: float | None = None
    include_trashed: bool = False


@dataclass
class SearchHit:
    asset_id: int
    score: float
    rank: int
    sources: list[str] = field(default_factory=list)
    fragment: str = ""          # 命中片段（<mark> 高亮）
    snippet: str = ""           # 纯文本片段


def fts_query_escape(q: str) -> str:
    """将用户输入转成安全的 FTS5 查询串。"""
    terms = [t for t in re.split(r"\s+", q.strip()) if t]
    if not terms:
        return ""
    parts = []
    for t in terms:
        safe = t.replace('"', '""')
        parts.append(f'"{safe}"')
    return " ".join(parts)


def build_asset_filter(f: SearchFilter, alias: str = "a") -> tuple[str, list[Any]]:
    """把组合过滤转成 SQL WHERE 子句（不含 id 条件）。供检索与导出共用。"""
    conds: list[str] = []
    params: list[Any] = []
    a = alias
    if not f.include_trashed:
        conds.append(f"{a}.deleted_at IS NULL")
    if f.date_from:
        conds.append(f"date({a}.created_at) >= ?")
        params.append(f.date_from)
    if f.date_to:
        conds.append(f"date({a}.created_at) <= ?")
        params.append(f.date_to)
    if f.min_width is not None:
        conds.append(f"{a}.width >= ?")
        params.append(f.min_width)
    if f.max_width is not None:
        conds.append(f"{a}.width <= ?")
        params.append(f.max_width)
    if f.min_height is not None:
        conds.append(f"{a}.height >= ?")
        params.append(f.min_height)
    if f.max_height is not None:
        conds.append(f"{a}.height <= ?")
        params.append(f.max_height)
    if f.min_confidence is not None:
        conds.append(f"{a}.ocr_confidence IS NOT NULL AND {a}.ocr_confidence >= ?")
        params.append(f.min_confidence)
    if f.max_confidence is not None:
        conds.append(f"{a}.ocr_confidence IS NOT NULL AND {a}.ocr_confidence <= ?")
        params.append(f.max_confidence)
    if f.tags:
        op = " OR " if f.tag_any else " AND "
        subs = []
        for tag in f.tags:
            subs.append(
                f"EXISTS (SELECT 1 FROM asset_tags at2 JOIN tags t ON t.id = at2.tag_id "
                f"WHERE at2.asset_id = {a}.id AND (t.name = ? OR t.name LIKE ?))"
            )
            params.extend([tag, tag.rstrip("/") + "/%"])
        conds.append("(" + op.join(subs) + ")")
    if f.custom_fields:
        for key, value in f.custom_fields.items():
            conds.append(
                f"EXISTS (SELECT 1 FROM custom_field_values cfv "
                f"JOIN custom_field_defs cfd ON cfd.id = cfv.field_id "
                f"WHERE cfv.asset_id = {a}.id AND cfd.key = ? AND cfv.value = ?)"
            )
            params.extend([key, value])
    if not conds:
        return "1=1", []
    return " AND ".join(conds), params


class HybridSearch:
    def __init__(self, db: Database, embedder: EmbeddingEngine | None = None,
                 notes_in_fts: bool = True):
        self.db = db
        self.embedder = embedder
        self.notes_in_fts = notes_in_fts

    # ------------------------------------------------------------------
    def _fts_ranked(self, query: str, limit: int) -> list[int]:
        """关键词检索：返回按相关度排序的 asset_id 列表。"""
        q = query.strip()
        if len(q) < _SHORT_QUERY:
            # 短查询（<3 字符）trigram 无法命中，退化为 LIKE 子串检索
            rows = self.db.query(
                "SELECT asset_id, SUM(LENGTH(text)) AS hits FROM ocr_texts "
                "WHERE text LIKE ? GROUP BY asset_id ORDER BY hits DESC, asset_id LIMIT ?",
                (f"%{q}%", limit),
            )
            return [r["asset_id"] for r in rows]

        fts = fts_query_escape(q)
        if not fts:
            return []
        rows = self.db.query(
            "SELECT asset_id, bm25(ocr_fts) AS score FROM ocr_fts "
            "WHERE ocr_fts MATCH ? ORDER BY score LIMIT ?",
            (fts, limit),
        )
        ranked, seen = [], set()
        for r in rows:
            if r["asset_id"] not in seen:
                ranked.append(r["asset_id"])
                seen.add(r["asset_id"])
        if self.notes_in_fts:
            note_rows = self.db.query(
                "SELECT asset_id, bm25(notes_fts) AS score FROM notes_fts "
                "WHERE notes_fts MATCH ? ORDER BY score LIMIT ?",
                (fts, limit),
            )
            for r in note_rows:
                if r["asset_id"] not in seen:
                    ranked.append(r["asset_id"])
                    seen.add(r["asset_id"])
        return ranked

    # ------------------------------------------------------------------
    def _vec_ranked(self, query: str, limit: int) -> list[int]:
        """向量检索：返回按相似度排序的 asset_id 列表。"""
        if self.embedder is None or not self.embedder.is_available():
            return []
        vec = self.embedder.embed_one(QUERY_INSTRUCTION + query)
        blob = vec.astype("<f4").tobytes()
        rows = self.db.query(
            "SELECT rowid FROM vec_embeddings WHERE embedding MATCH ? AND k = ?",
            (blob, limit),
        )
        return [r["rowid"] for r in rows]

    # ------------------------------------------------------------------
    def search(
        self,
        query: str,
        k: int = 20,
        filters: SearchFilter | None = None,
    ) -> list[SearchHit]:
        """混合搜索：FTS5 + 向量 + RRF 融合 + 组合过滤（默认排除回收站）。"""
        fts_ranked = self._fts_ranked(query, _FTS_LIMIT)
        vec_ranked = self._vec_ranked(query, _VEC_LIMIT)

        rrf = RRF(k=_RRF_K)
        if fts_ranked:
            rrf.add_ranking("fts", fts_ranked)
        if vec_ranked:
            rrf.add_ranking("vec", vec_ranked)
        if not fts_ranked and not vec_ranked:
            return []
        fused = rrf.fuse()
        filters = filters or SearchFilter()

        hits: list[SearchHit] = []
        rank = 0
        for aid, score in fused:
            if filters and not self._passes_filters(aid, filters):
                continue
            rank += 1
            hits.append(SearchHit(
                asset_id=aid,
                score=round(score, 6),
                rank=rank,
                sources=rrf.sources_of(aid),
                fragment=self._fragment(aid, query),
                snippet=self._snippet(aid),
            ))
            if rank >= k:
                break
        return hits

    # ------------------------------------------------------------------
    def similar(self, asset_id: int, k: int = 10) -> list[SearchHit]:
        """以图搜图：用目标资产的向量在库内找最相似截图。"""
        if self.embedder is None or not self.embedder.is_available():
            return []
        row = self.db.query_one(
            "SELECT embedding FROM embeddings WHERE asset_id=? AND dim>0",
            (asset_id,),
        )
        if row is None or not row["embedding"]:
            return []
        rows = self.db.query(
            "SELECT rowid, distance FROM vec_embeddings "
            "WHERE embedding MATCH ? AND k = ? AND rowid != ?",
            (row["embedding"], k + 1, asset_id),
        )
        hits = []
        for rank, r in enumerate(rows, start=1):
            if r["rowid"] == asset_id:
                continue
            hits.append(SearchHit(
                asset_id=r["rowid"],
                score=round(1.0 / (1.0 + float(r["distance"])), 6),
                rank=rank,
                sources=["vec"],
                snippet=self._snippet(r["rowid"]),
            ))
            if len(hits) >= k:
                break
        return hits

    # ------------------------------------------------------------------
    def _passes_filters(self, asset_id: int, f: SearchFilter) -> bool:
        clause, params = build_asset_filter(f)
        sql = f"SELECT COUNT(*) FROM assets a WHERE a.id = ? AND {clause}"
        return bool(self.db.scalar(sql, (asset_id, *params)))

    # ------------------------------------------------------------------
    def _fragment(self, asset_id: int, query: str) -> str:
        """取包含命中的 OCR 文本窗口，关键词 <mark> 高亮。"""
        terms = [t for t in re.split(r"\s+", query.strip()) if t]
        rows = self.db.query(
            "SELECT text FROM ocr_texts WHERE asset_id=? ORDER BY chunk_index",
            (asset_id,),
        )
        best, best_pos = "", -1
        for row in rows:
            text = row["text"]
            for t in terms:
                pos = text.lower().find(t.lower())
                if pos >= 0 and (best_pos < 0 or pos < best_pos):
                    best, best_pos = text, pos
        if best_pos < 0:
            return ""
        start = max(0, best_pos - 25)
        end = min(len(best), best_pos + len(terms[0]) + 40)
        window = best[start:end]
        for t in terms:
            window = re.sub(
                re.escape(t), lambda m: f"<mark>{m.group(0)}</mark>",
                window, flags=re.IGNORECASE,
            )
        return ("…" if start > 0 else "") + window + ("…" if end < len(best) else "")

    def _snippet(self, asset_id: int) -> str:
        rows = self.db.query(
            "SELECT text FROM ocr_texts WHERE asset_id=? ORDER BY chunk_index LIMIT 5",
            (asset_id,),
        )
        return "\n".join(r["text"] for r in rows)[:200]
