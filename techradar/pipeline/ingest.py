"""Ingest RawItems: canonical_key → upsert items/item_sources → snapshot → health.

Concurrency: items/item_sources use INSERT … ON CONFLICT so two workers ingesting the same
key never raise; the loser simply updates.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone

from sqlalchemy import literal_column, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import Session

from ..fetchers.base import FetchResult, RawItem
from ..models import Item, ItemSource, Snapshot, SourceHealth
from .canonical import canonical_key, expand_short_url, normalize_url

log = logging.getLogger(__name__)

GENERIC_KINDS = ("other", "article", "post")
CANONICAL_KIND_WINS = ("repo", "paper", "release")


@dataclass
class IngestStats:
    source: str
    received: int = 0
    new_items: int = 0
    merged_items: int = 0   # RawItem attached to an existing item (cross-source or re-seen)
    new_sources: int = 0
    snapshots: int = 0
    errors: list[str] = field(default_factory=list)


def _resolve_kind(fetcher_kind: str, canonical_kind: str) -> str:
    if canonical_kind in CANONICAL_KIND_WINS:
        return canonical_kind
    if fetcher_kind not in GENERIC_KINDS:
        return fetcher_kind
    return canonical_kind if canonical_kind != "article" else fetcher_kind


def ingest_raw(session: Session, raw: RawItem, now: datetime | None = None,
               expand_short: bool = True) -> tuple[Item, bool, bool]:
    """Returns (item, item_created, source_created)."""
    now = now or datetime.now(timezone.utc)
    url = expand_short_url(raw.url) if expand_short else raw.url
    key, ckind = canonical_key(url)
    kind = _resolve_kind(raw.kind, ckind)
    n_url = normalize_url(url)

    # ---- item upsert (race-safe) ----
    ins = pg_insert(Item).values(
        canonical_key=key, kind=kind, title=raw.title[:500], url=n_url, lang=raw.lang,
        published_at=raw.published_at, first_seen_at=now, last_seen_at=now,
        content=raw.content, content_level=raw.content_level, status="new",
    )
    stmt = ins.on_conflict_do_update(
        index_elements=[Item.canonical_key],
        set_={
            "last_seen_at": now,
            # earliest published_at wins
            "published_at": _least(Item.published_at, ins.excluded.published_at),
            # richer content wins
            "content": _case_content(Item, ins.excluded),
            "content_level": _greatest(Item.content_level, ins.excluded.content_level),
            # canonical kind upgrade
            "kind": _case_kind(Item, ins.excluded),
        },
    ).returning(Item.id, literal_column("(xmax = 0)"))
    row = session.execute(stmt).first()
    item_id, created = row[0], bool(row[1])
    item = session.get(Item, item_id)

    # ---- item_source upsert ----
    raw_json = {**raw.raw, "tags_hint": raw.tags_hint} if raw.tags_hint else raw.raw
    sins = pg_insert(ItemSource).values(
        item_id=item_id, source=raw.source, external_id=raw.external_id, source_url=raw.source_url,
        author=raw.author, author_key=raw.author_key, metrics_raw=raw.metrics, seen_at=now, raw=raw_json,
    ).on_conflict_do_update(
        constraint="uq_item_sources_source_ext",
        set_={"metrics_raw": raw.metrics, "seen_at": now, "item_id": item_id},  # re-point if canonical rules changed
    ).returning(ItemSource.id, literal_column("(xmax = 0)"))
    prev_item_id = session.scalar(
        select(ItemSource.item_id).where(ItemSource.source == raw.source, ItemSource.external_id == raw.external_id)
    )
    srow = session.execute(sins).first()
    src_id, src_created = srow[0], bool(srow[1])
    if prev_item_id is not None and prev_item_id != item_id:
        _migrate_item_refs(session, prev_item_id, item_id)

    if raw.metrics:
        session.execute(
            pg_insert(Snapshot).values(item_source_id=src_id, ts=now, metrics=raw.metrics).on_conflict_do_nothing()
        )
    session.flush()
    session.refresh(item)
    return item, created, src_created


# ---- SQL helper expressions ------------------------------------------------
from sqlalchemy import case, func  # noqa: E402


def _least(a, b):
    return func.least(func.coalesce(a, b), func.coalesce(b, a))


def _greatest(a, b):
    return func.greatest(func.coalesce(a, 0), func.coalesce(b, 0))


def _case_content(tbl, exc):
    return case((func.coalesce(exc.content_level, 0) > func.coalesce(tbl.content_level, 0), exc.content), else_=tbl.content)


def _case_kind(tbl, exc):
    return case(
        (exc.kind.in_(CANONICAL_KIND_WINS), exc.kind),
        (tbl.kind.in_(GENERIC_KINDS) & ~exc.kind.in_(GENERIC_KINDS), exc.kind),
        else_=tbl.kind,
    )


def _migrate_item_refs(session: Session, old_id: int, new_id: int) -> None:
    """Canonical rules changed → a source now points at a different item. Move user data along."""
    from sqlalchemy import update
    from ..models import DigestItem, EntityTimeline, Feedback
    session.execute(update(Feedback).where(Feedback.item_id == old_id).values(item_id=new_id))
    for tbl in (DigestItem, EntityTimeline):
        # composite/unique keys: delete conflicts first, then move
        rows = session.execute(select(tbl).where(tbl.item_id == old_id)).scalars().all()
        for r in rows:
            r.item_id = new_id
    session.flush()


def _upsert_health(session: Session, source: str, *, ok: bool, calls: int = 0, items: int | None = None,
                   error: str | None = None, month_budget: int | None = None) -> None:
    now = datetime.now(timezone.utc)
    mk = now.strftime("%Y-%m")
    h = session.get(SourceHealth, source)
    if h is None:
        h = SourceHealth(source=source, consecutive_failures=0, month_calls=0)
        session.add(h)
    if h.month_key != mk:
        h.month_key, h.month_calls = mk, 0
    h.month_calls = (h.month_calls or 0) + calls
    if month_budget is not None:
        h.month_budget = month_budget
    h.last_run_at = now
    if ok:
        h.last_success_at = now
        h.last_items = items
        h.consecutive_failures = 0
        h.last_error = None
    else:
        h.consecutive_failures = (h.consecutive_failures or 0) + 1
        h.last_error = (error or "")[:500]


def record_health(session: Session, res: FetchResult, items_ingested: int) -> None:
    budget = res.month_budget
    if budget is None:
        from ..fetchers.base import registry
        cls = registry.get(res.source)
        budget = getattr(cls, "month_budget", None) if cls else None
    _upsert_health(session, res.source, ok=res.ok, calls=res.calls, items=items_ingested,
                   error=res.error, month_budget=budget)
    # per-sub-source rows (e.g. rss:<feed_id>)
    per_sub: dict[str, int] = {}
    for it in res.items:
        if it.source != res.source:
            per_sub[it.source] = per_sub.get(it.source, 0) + 1
    for sub in res.ok_subsources:
        per_sub.setdefault(sub, 0)
    for sub, n in per_sub.items():
        _upsert_health(session, sub, ok=True, items=n)
    for err in res.partial_errors:
        sub, _, msg = err.partition(": ")
        if sub and sub != res.source:
            _upsert_health(session, sub, ok=False, error=msg)


def budget_check(session: Session, source: str, budget: int) -> bool:
    if not budget:
        return True
    h = session.get(SourceHealth, source)
    if h is None:
        return True
    mk = datetime.now(timezone.utc).strftime("%Y-%m")
    if h.month_key != mk:
        return True
    return (h.month_calls or 0) < budget


def ingest_result(session: Session, res: FetchResult) -> IngestStats:
    st = IngestStats(source=res.source, received=len(res.items))
    now = datetime.now(timezone.utc)
    for raw in res.items:
        try:
            with session.begin_nested():
                _, created, src_created = ingest_raw(session, raw, now)
                st.new_items += int(created)
                st.merged_items += int(not created)
                st.new_sources += int(src_created)
                st.snapshots += int(bool(raw.metrics))
        except Exception as e:  # noqa: BLE001
            log.exception("ingest failed for %s/%s", raw.source, raw.external_id)
            st.errors.append(f"{raw.external_id}: {e}"[:200])
    record_health(session, res, st.new_items + st.merged_items)
    return st
