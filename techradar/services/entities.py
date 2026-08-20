"""Entity registry: sync whitelist from subscriptions.yaml, maintain timeline from matched items."""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session, selectinload

from ..models import Entity, EntityAlias, EntityTimeline, Item
from ..settings import get_subscriptions

log = logging.getLogger(__name__)
KIND_EVENT = {"release": "release", "paper": "paper", "repo": "repo", "post": "discussion", "article": "mention"}


def sync_entities(session: Session) -> int:
    """Upsert whitelist entities + aliases from config. Returns count."""
    n = 0
    for e in get_subscriptions().entities:
        ent = session.scalar(select(Entity).where(Entity.canonical_name == e.name))
        if ent is None:
            ent = Entity(canonical_name=e.name, type=e.type, anchors=e.anchors,
                         first_seen_at=datetime.now(timezone.utc))
            session.add(ent)
            session.flush()
        else:
            ent.type = e.type
            ent.anchors = e.anchors
        for alias in {e.name.lower(), *[a.lower() for a in e.aliases]}:
            session.execute(pg_insert(EntityAlias).values(alias=alias, entity_id=ent.id).on_conflict_do_nothing())
        n += 1
    return n


def update_timeline(session: Session, hours: int = 96) -> int:
    """Add timeline rows for recently seen items whose entities_matched hit registry entities."""
    from datetime import timedelta
    since = datetime.now(timezone.utc) - timedelta(hours=hours)
    ents = {e.canonical_name: e.id for e in session.scalars(select(Entity)).all()}
    if not ents:
        return 0
    items = session.scalars(
        select(Item).where(Item.last_seen_at > since, Item.entities_matched.isnot(None))
    ).all()
    n = 0
    for it in items:
        for name in it.entities_matched or []:
            eid = ents.get(name)
            if not eid:
                continue
            res = session.execute(
                pg_insert(EntityTimeline).values(
                    entity_id=eid, item_id=it.id, event_type=KIND_EVENT.get(it.kind, "mention"),
                    ts=it.published_at or it.first_seen_at,
                ).on_conflict_do_nothing().returning(EntityTimeline.id)   # returns a row only when inserted
            )
            n += len(res.all())
    return n


def entity_overview(session: Session, entity: Entity, limit: int = 30) -> dict:
    rows = session.execute(
        select(EntityTimeline, Item).join(Item, Item.id == EntityTimeline.item_id)
        .where(EntityTimeline.entity_id == entity.id).order_by(EntityTimeline.ts.desc()).limit(limit)
    ).all()
    timeline = [{
        "ts": tl.ts.isoformat()[:10], "event": tl.event_type, "item_id": it.id,
        "title": it.title, "summary": it.summary_one, "url": it.url,
        "entities": [e for e in (it.entities_matched or []) if e != entity.canonical_name],
    } for tl, it in rows]
    return {"id": entity.id, "name": entity.canonical_name, "type": entity.type, "anchors": entity.anchors or {},
            "watched": entity.watched, "notes": entity.notes, "timeline": timeline,
            "brief": entity.brief or {},
            "brief_at": entity.brief_at.isoformat()[:16] if entity.brief_at else None,
            "first_seen": entity.first_seen_at.isoformat()[:10] if entity.first_seen_at else None}
