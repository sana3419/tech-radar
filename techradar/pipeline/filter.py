"""Rule filter: new → queued (worth enriching/showing) or filtered.

Pass if: any subscription hit (topic/author/entity) OR platform heat percentile ≥ threshold.
Stores hits in score_breakdown["hits"] so score/digest can explain "why".
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Item
from .heat import build_heat_model
from .lifecycle import transition
from .matching import match_item

HEAT_THRESHOLD = 0.85   # top 15% inside its platform


@dataclass
class FilterStats:
    scanned: int = 0
    queued: int = 0
    filtered: int = 0


def run_filter(session: Session, heat_threshold: float = HEAT_THRESHOLD, limit: int = 2000) -> FilterStats:
    st = FilterStats()
    heat = build_heat_model(session)
    items = session.scalars(
        select(Item).options(selectinload(Item.sources)).where(Item.status == "new")
        .order_by(Item.first_seen_at.desc()).limit(limit)
    ).all()
    for it in items:
        st.scanned += 1
        hits = match_item(it)
        pct, heat_src = heat.item_heat(it)
        passed = hits.any or pct >= heat_threshold
        bd = dict(it.score_breakdown or {})
        bd["hits"] = hits.to_json()
        bd["heat_pct"] = round(pct, 3)
        bd["heat_src"] = heat_src
        it.score_breakdown = bd
        transition(session, it, "queued" if passed else "filtered")
        st.queued += int(passed)
        st.filtered += int(not passed)
    return st


def rescue_filtered(session: Session, days: int = 3) -> int:
    """After subscription changes: re-evaluate recently filtered items."""
    from datetime import timedelta
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    items = session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.status == "filtered", Item.first_seen_at > cutoff)
    ).all()
    n = 0
    for it in items:
        if match_item(it).any:
            transition(session, it, "queued")
            n += 1
    return n
