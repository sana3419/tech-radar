"""View-model helpers for the web UI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..digest.daily import DigestData, load_persisted, select_digest
from ..models import Entity, EntityTimeline, Feedback, Item
from .health import alerts as health_alerts
from .items import item_to_dict


def _acted_ids(session: Session):
    return select(Feedback.item_id).where(Feedback.action.in_(("read", "ignore", "save")))


def home_digest(session: Session) -> dict:
    """Today's digest (persisted if sent; else preview selection) as numbered card list."""
    d = load_persisted(session)
    preview = False
    if d is None:
        d = select_digest(session)
        preview = True
    acted = {r[0] for r in session.execute(_acted_ids(session)).all()}
    cards, n = [], 0
    for section, items in (("top", d.top), ("folded", d.folded)):
        for it in items:
            n += 1
            c = item_to_dict(it)
            c["digest_no"] = n
            c["section"] = section
            c["acted"] = it.id in acted
            c["extra_sources"] = sorted(d.event_sources.get(it.id, set())) if getattr(d, "event_sources", None) else []
            c["explore"] = it.id in (d.explore or set())
            cards.append(c)
    return {"cards": cards, "preview": preview, "explore": d.explore}


def unread_rest(session: Session, exclude_ids: set[int], offset: int = 0, limit: int = 20) -> list[dict]:
    """Scored/digested unread items beyond the digest, score-desc, event-deduped, paged."""
    acted = _acted_ids(session)
    rows = list(session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.status.in_(("scored", "digested")), ~Item.id.in_(acted), Item.score.isnot(None))
        .order_by(Item.score.desc()).offset(0).limit(400)
    ).all())
    seen_events, out = set(), []
    for it in rows:
        if it.id in exclude_ids:
            if it.event_id:
                seen_events.add(it.event_id)
            continue
        if it.event_id:
            if it.event_id in seen_events:
                continue
            seen_events.add(it.event_id)
        out.append(it)
    return [item_to_dict(x) for x in out[offset:offset + limit]]


def unread_count(session: Session) -> int:
    acted = _acted_ids(session)
    return session.scalar(
        select(func.count()).select_from(Item)
        .where(Item.status.in_(("scored", "digested")), ~Item.id.in_(acted))
    ) or 0


def sidebar(session: Session) -> dict:
    now = datetime.now(timezone.utc)
    since = now - timedelta(hours=48)
    watched = []
    for e in session.scalars(select(Entity).where(Entity.watched.is_(True))).all():
        rows = session.execute(
            select(EntityTimeline, Item).join(Item, Item.id == EntityTimeline.item_id)
            .where(EntityTimeline.entity_id == e.id, EntityTimeline.ts >= since)
            .order_by(EntityTimeline.ts.desc()).limit(3)
        ).all()
        if rows:
            watched.append({"name": e.canonical_name, "items": [
                {"id": it.id, "text": (it.summary_one or it.title)[:44], "url": it.url} for _, it in rows]})
    # rising events: grouped items in window with ≥2 source families
    ev_rows = session.execute(
        select(Item.event_id, func.count(func.distinct(Item.id)))
        .where(Item.event_id.isnot(None), Item.last_seen_at >= since, Item.status != "filtered")
        .group_by(Item.event_id).having(func.count(func.distinct(Item.id)) >= 2)
        .order_by(func.count(func.distinct(Item.id)).desc()).limit(6)
    ).all()
    rising = []
    for eid, cnt in ev_rows:
        best = session.scalar(select(Item).options(selectinload(Item.sources))
                              .where(Item.event_id == eid).order_by(Item.score.desc().nullslast()).limit(1))
        if best:
            rising.append({"id": best.id, "text": (best.summary_one or best.title)[:44], "url": best.url, "n": cnt})
    return {"watched": watched, "rising": rising, "alerts": health_alerts(session)}
