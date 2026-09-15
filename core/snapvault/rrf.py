"""RRF（Reciprocal Rank Fusion）融合排序。

score(asset) = Σ_source 1 / (k + rank_source(asset))
k 默认 60，对排名（而非分值）融合，天然可混合异质检索结果（FTS BM25 与向量距离）。
"""
from __future__ import annotations

from dataclasses import dataclass, field

DEFAULT_K = 60


@dataclass
class RRF:
    k: int = DEFAULT_K
    _scores: dict[int, float] = field(default_factory=dict)
    _sources: dict[int, list[str]] = field(default_factory=dict)

    def add_ranking(self, source: str, asset_ids: list[int]) -> None:
        """加入一个来源的排序结果（第 1 位 rank=1）。"""
        for rank, aid in enumerate(asset_ids, start=1):
            self._scores[aid] = self._scores.get(aid, 0.0) + 1.0 / (self.k + rank)
            self._sources.setdefault(aid, []).append(source)

    def fuse(self, limit: int | None = None) -> list[tuple[int, float]]:
        ranked = sorted(self._scores.items(), key=lambda kv: (-kv[1], kv[0]))
        return ranked if limit is None else ranked[:limit]

    def sources_of(self, asset_id: int) -> list[str]:
        return self._sources.get(asset_id, [])


def normalize_ranks(ranked: list[tuple[int, float]], ascending: bool = True) -> list[int]:
    """按分值排序返回 id 列表（ascending=True 表示分值越小越靠前）。"""
    return [aid for aid, _ in sorted(ranked, key=lambda kv: kv[1], reverse=not ascending)]
