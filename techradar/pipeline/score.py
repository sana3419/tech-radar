"""P0 rule ranker (docs/02 §4):

score = (w_sub*sub_hit + w_heat*heat_pct + w_author*author_w + w_cross*cross - decay) * pref_mult
All factors stored in score_breakdown; reasons rendered for the digest.
"""
from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Item
from .heat import build_heat_model
from .lifecycle import transition
from .matching import RuleHits, match_item
from .preferences import load_pref_model

RANKER_VERSION = "p0-rules-1"
W = {"sub": 3.0, "heat": 1.0, "author": 1.0, "cross": 0.5, "decay": 0.5}
SOURCE_LABEL = {"hackernews": "HN", "github": "GitHub", "rss": "RSS", "arxiv": "arXiv", "x": "X", "wechat": "公众号"}


@dataclass
class ScoreStats:
    scored: int = 0


def _age_hours(item: Item, now: datetime) -> float:
    ref = item.published_at or item.first_seen_at
    return max((now - ref).total_seconds() / 3600.0, 0.0)


def _reasons(hits: RuleHits, heat_pct: float, heat_src: str | None, n_sources: int, author_w: float) -> list[str]:
    r: list[str] = []
    for t in hits.topics[:2]:
        r.append(f"命中订阅: {t['label']}")
    for e in hits.entities[:2]:
        r.append(f"关注实体: {e}")
    if author_w > 0:
        r.append("作者白名单: " + ", ".join(a["key"] for a in hits.authors[:2]))
    if heat_pct >= 0.95 and heat_src:
        r.append(f"{SOURCE_LABEL.get(heat_src.split(':')[0], heat_src)} 热度 Top 5%")
    elif heat_pct >= 0.85 and heat_src:
        r.append(f"{SOURCE_LABEL.get(heat_src.split(':')[0], heat_src)} 热度 Top 15%")
    if n_sources >= 2:
        r.append(f"{n_sources} 个来源同时出现")
    return r


def score_item(item: Item, heat, prefs, now: datetime) -> None:
    hits = match_item(item)
    heat_pct, heat_src = heat.item_heat(item)
    sub_hit = hits.max_boost                # 0 if none, else max boost (≥1 typically)
    author_w = hits.max_author_weight
    n_sources = len({s.source for s in item.sources})
    cross = math.log1p(n_sources - 1) if n_sources > 1 else 0.0
    age_h = _age_hours(item, now)
    decay = math.log1p(age_h / 24.0)
    base = W["sub"] * sub_hit + W["heat"] * heat_pct + W["author"] * author_w + W["cross"] * cross - W["decay"] * decay
    mult, pref_detail = prefs.item_multiplier(item)
    score = base * mult if base > 0 else base * (2.0 - mult)   # negative base: mult<1 pushes further down
    item.score = round(score, 4)
    item.score_breakdown = {
        "hits": hits.to_json(),
        "sub_hit": sub_hit, "heat_pct": round(heat_pct, 3), "heat_src": heat_src,
        "author_w": author_w, "n_sources": n_sources, "cross": round(cross, 3),
        "age_hours": round(age_h, 1), "decay": round(decay, 3),
        "base": round(base, 4), "pref_mult": round(mult, 3), "pref_detail": pref_detail,
        "weights": W,
    }
    item.ranker_version = RANKER_VERSION
    item.reasons = _reasons(hits, heat_pct, heat_src, n_sources, author_w)
    if hits.entities:
        item.entities_matched = sorted(set((item.entities_matched or []) + hits.entities))


def run_score(session: Session, hours: int = 72, now: datetime | None = None) -> ScoreStats:
    now = now or datetime.now(timezone.utc)
    heat = build_heat_model(session, now=now)
    prefs = load_pref_model(session, now)
    items = session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.status.in_(("queued", "enriched", "scored", "digested")),
               Item.first_seen_at > now - timedelta(hours=hours))
    ).all()
    st = ScoreStats()
    for it in items:
        score_item(it, heat, prefs, now)
        if it.status in ("queued", "enriched"):
            transition(session, it, "scored")
        st.scored += 1
    return st


def top_items(session: Session, n: int = 20, statuses=("scored", "digested")) -> list[Item]:
    return list(session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.status.in_(statuses)).order_by(Item.score.desc().nullslast()).limit(n)
    ).all())
