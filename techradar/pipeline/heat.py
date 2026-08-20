"""Platform-internal heat percentile: rolling 14-day log1p(metric) distribution per source."""
from __future__ import annotations

import bisect
import math
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Item, ItemSource

PRIMARY_METRIC = {"hackernews": "points", "github": "stars", "reddit": "score", "x": "likes"}
NEUTRAL = 0.5


class HeatModel:
    def __init__(self, dists: dict[str, list[float]]):
        self.dists = dists  # source → sorted log1p values

    @staticmethod
    def source_key(source: str) -> str:
        return source.split(":")[0]

    def metric_of(self, src: ItemSource) -> float | None:
        key = self.source_key(src.source)
        m = PRIMARY_METRIC.get(key)
        if not m or not src.metrics_raw:
            return None
        v = src.metrics_raw.get(m)
        return float(v) if v is not None else None

    def percentile(self, src: ItemSource) -> float | None:
        v = self.metric_of(src)
        if v is None:
            return None
        dist = self.dists.get(self.source_key(src.source))
        if not dist or len(dist) < 20:
            return NEUTRAL
        x = math.log1p(max(v, 0))
        return bisect.bisect_right(dist, x) / len(dist)

    def item_heat(self, item: Item) -> tuple[float, str | None]:
        """Max percentile across sources; returns (pct, source_with_max). Neutral if no metric sources."""
        best, best_src = None, None
        for s in item.sources:
            p = self.percentile(s)
            if p is not None and (best is None or p > best):
                best, best_src = p, s.source
        return (best if best is not None else NEUTRAL), best_src


def build_heat_model(session: Session, days: int = 14, now: datetime | None = None) -> HeatModel:
    now = now or datetime.now(timezone.utc)
    rows = session.execute(
        select(ItemSource.source, ItemSource.metrics_raw)
        .join(Item, Item.id == ItemSource.item_id)
        .where(Item.first_seen_at > now - timedelta(days=days))
    ).all()
    dists: dict[str, list[float]] = {}
    for source, metrics in rows:
        key = HeatModel.source_key(source)
        m = PRIMARY_METRIC.get(key)
        if not m or not metrics or metrics.get(m) is None:
            continue
        dists.setdefault(key, []).append(math.log1p(max(float(metrics[m]), 0)))
    for k in dists:
        dists[k].sort()
    return HeatModel(dists)
