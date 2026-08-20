"""Item status machine and expiry.

new ──rule_filter──▶ queued ──enrich──▶ enriched ──score──▶ scored ──digest──▶ digested
  └──rule_filter──▶ filtered (dropped, never shown)
scored/digested ──48h no read/save──▶ expired         any ──user read──▶ (status kept, feedback row)
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import exists, select, update
from sqlalchemy.orm import Session

from ..models import Feedback, Item

STATUSES = ("new", "filtered", "queued", "enriched", "scored", "digested", "expired")
TRANSITIONS = {
    "new": {"queued", "filtered"},
    "queued": {"enriched", "filtered", "scored", "expired"},   # expired: user ignored a near-duplicate
    "enriched": {"scored", "enriched"},
    "scored": {"digested", "expired", "scored"},
    "digested": {"expired", "digested", "scored"},   # 72h re-score may touch digested items
    "filtered": {"queued"},      # subscription change may rescue
    "expired": set(),
}


def transition(session: Session, item: Item, to: str) -> None:
    if to not in TRANSITIONS.get(item.status, set()):
        raise ValueError(f"illegal transition {item.status} -> {to} for item {item.id}")
    item.status = to


def expire_unread(session: Session, hours: int = 48, now: datetime | None = None) -> int:
    """scored/digested items older than `hours` with no read/save feedback → expired."""
    now = now or datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    from ..models import Digest, DigestItem
    kept = exists(
        select(Feedback.id).where(Feedback.item_id == Item.id, Feedback.action.in_(("read", "save", "dig")))
    )
    recently_digested = exists(
        select(DigestItem.item_id).join(Digest, Digest.id == DigestItem.digest_id)
        .where(DigestItem.item_id == Item.id, Digest.sent_at.isnot(None), Digest.sent_at >= cutoff)
    )
    res = session.execute(
        update(Item)
        .where(Item.status.in_(("scored", "digested")), Item.first_seen_at < cutoff, ~kept, ~recently_digested)
        .values(status="expired")
    )
    return res.rowcount
