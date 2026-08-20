"""Subscription tuning stats: per-topic hits & engagement (7d), per-source volume, muted prefs."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from ..models import Feedback, Item, ItemSource, Preference
from ..settings import get_subscriptions


def topic_stats(session: Session, days: int = 7) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(text("""
        SELECT t->>'name' AS topic, count(*) AS hits,
               count(*) FILTER (WHERE i.status = 'digested') AS pushed,
               count(*) FILTER (WHERE fb.actions @> ARRAY['save']) AS saved,
               count(*) FILTER (WHERE fb.actions @> ARRAY['ignore']) AS ignored,
               count(*) FILTER (WHERE fb.actions && ARRAY['click','read','dig']) AS engaged
        FROM items i
        CROSS JOIN LATERAL jsonb_array_elements(i.score_breakdown->'hits'->'topics') AS t
        LEFT JOIN LATERAL (
            SELECT array_agg(DISTINCT action) AS actions FROM feedback f WHERE f.item_id = i.id
        ) fb ON true
        WHERE i.first_seen_at > :since
        GROUP BY 1 ORDER BY hits DESC
    """), {"since": since}).mappings().all()
    known = {t.name: t for t in get_subscriptions().topics}
    out = []
    for r in rows:
        t = known.get(r["topic"])
        out.append({**dict(r), "label": (t.label if t else r["topic"]),
                    "boost": (t.boost if t else None),
                    "queries": (t.queries if t else [])})
    # topics with zero hits
    for name, t in known.items():
        if not any(r["topic"] == name for r in rows):
            out.append({"topic": name, "label": t.label or name, "hits": 0, "pushed": 0, "saved": 0,
                        "ignored": 0, "engaged": 0, "boost": t.boost, "queries": t.queries})
    return out


def source_stats(session: Session, days: int = 7) -> list[dict]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.execute(
        select(ItemSource.source, func.count(func.distinct(ItemSource.item_id)),
               func.count(func.distinct(Item.id)).filter(Item.status == "digested"),
               func.count(func.distinct(Item.id)).filter(Item.status == "filtered"))
        .join(Item, Item.id == ItemSource.item_id).where(Item.first_seen_at > since)
        .group_by(ItemSource.source).order_by(func.count(func.distinct(ItemSource.item_id)).desc())
    ).all()
    return [{"source": r[0], "items": r[1], "pushed": r[2], "filtered": r[3]} for r in rows]


def muted(session: Session) -> list[dict]:
    now = datetime.now(timezone.utc)
    rows = session.scalars(select(Preference).where(Preference.muted_until.isnot(None))).all()
    return [{"kind": p.kind, "key": p.key, "until": p.muted_until.isoformat()[:16],
             "active": p.muted_until > now} for p in rows]
