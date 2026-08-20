"""Tiered metric revisit: <24h every hour, <7d every 6h, older: stop. Only for sources with metrics."""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..fetchers.base import get_fetcher
from ..models import Item, ItemSource, Snapshot, SourceHealth

log = logging.getLogger(__name__)
REVISIT_SOURCES = ("hackernews", "github")


def due_item_sources(session: Session, now: datetime | None = None, limit: int = 300,
                     source: str | None = None) -> list[ItemSource]:
    """`limit` applies per source when `source` is given; callers iterate REVISIT_SOURCES for fairness."""
    now = now or datetime.now(timezone.utc)
    if source is None:   # fair: `limit` per source
        out: list[ItemSource] = []
        for src in REVISIT_SOURCES:
            out.extend(due_item_sources(session, now, limit, source=src))
        return out
    sources = (source,)
    last_snap = (
        select(Snapshot.item_source_id, func.max(Snapshot.ts).label("last_ts"))
        .group_by(Snapshot.item_source_id).subquery()
    )
    q = (
        select(ItemSource)
        .join(Item, Item.id == ItemSource.item_id)
        .outerjoin(last_snap, last_snap.c.item_source_id == ItemSource.id)
        .where(ItemSource.source.in_(sources), Item.first_seen_at > now - timedelta(days=7))
        .where(
            (last_snap.c.last_ts.is_(None))
            | ((Item.first_seen_at > now - timedelta(hours=24)) & (last_snap.c.last_ts < now - timedelta(minutes=55)))
            | ((Item.first_seen_at <= now - timedelta(hours=24)) & (last_snap.c.last_ts < now - timedelta(hours=6)))
        )
        .order_by(last_snap.c.last_ts.asc().nullsfirst())
        .limit(limit)
    )
    return list(session.scalars(q).all())


def refresh_snapshots(session: Session, now: datetime | None = None, limit: int = 300) -> dict[str, int]:
    """Returns {source: n_snapshots_written}."""
    now = now or datetime.now(timezone.utc)
    from ..settings import get_settings
    from .ingest import _upsert_health
    written: dict[str, int] = {}
    for source in REVISIT_SOURCES:
        per_limit = limit
        if source == "github" and not get_settings().github_token:
            per_limit = min(limit, 40)   # unauthenticated core API: 60/h
        rows = due_item_sources(session, now, per_limit, source=source)
        if not rows:
            continue
        f = get_fetcher(source)
        if not hasattr(f, "refresh_metrics"):
            continue
        try:
            metrics_by_ext = f.refresh_metrics([r.external_id for r in rows])
            errs = f.config.pop("_errors", None) or []
            _upsert_health(session, f"{source}:refresh", ok=not errs or bool(metrics_by_ext), calls=f._calls,
                           items=len(metrics_by_ext), error="; ".join(errs)[:300] if errs else None)
            h = session.get(SourceHealth, source)
            if h is not None:
                h.month_calls = (h.month_calls or 0) + f._calls
        except Exception as e:  # noqa: BLE001
            log.exception("refresh_metrics failed for %s", source)
            _upsert_health(session, f"{source}:refresh", ok=False, calls=getattr(f, "_calls", 0), error=str(e))
            continue
        n = 0
        for r in rows:
            m = metrics_by_ext.get(r.external_id)
            if not m:
                continue
            if m != (r.metrics_raw or {}):
                r.metrics_raw = m
            session.execute(
                pg_insert(Snapshot).values(item_source_id=r.id, ts=now, metrics=m).on_conflict_do_nothing()
            )
            n += 1
        written[source] = n
    return written
