"""Feedback service: write feedback + feature snapshot, update preferences, immediate effects."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from sqlalchemy import select, update
from sqlalchemy.orm import Session, selectinload

from ..models import Feedback, FeedbackFeatures, Item, ItemSource, Preference, DigestItem, Digest
from ..pipeline.preferences import apply_feedback_to_prefs

VALID_ACTIONS = {"save", "ignore", "read", "click", "expand", "dig", "unsave"}


def _rank_in_latest_digest(session: Session, item_id: int) -> int | None:
    row = session.execute(
        select(DigestItem.position).join(Digest, Digest.id == DigestItem.digest_id)
        .where(DigestItem.item_id == item_id).order_by(Digest.day.desc()).limit(1)
    ).first()
    return row[0] if row else None


def record_feedback(session: Session, item_id: int, action: str, channel: str = "web",
                    note: str | None = None) -> dict:
    if action not in VALID_ACTIONS:
        raise ValueError(f"invalid action {action}")
    item = session.scalar(select(Item).options(selectinload(Item.sources)).where(Item.id == item_id))
    if item is None:
        raise KeyError(f"item {item_id} not found")
    fb = Feedback(item_id=item_id, action=action, channel=channel, note=note)
    session.add(fb)
    session.flush()
    session.add(FeedbackFeatures(
        feedback_id=fb.id, ranker_version=item.ranker_version, score=item.score,
        score_breakdown=item.score_breakdown, rank_in_digest=_rank_in_latest_digest(session, item_id),
        tags=item.tags, sources=[s.source for s in item.sources],
    ))
    apply_feedback_to_prefs(session, item, action)
    hidden = 0
    if action == "ignore":
        hidden = hide_similar(session, item)
    return {"feedback_id": fb.id, "item_id": item_id, "action": action, "hidden_similar": hidden}


def hide_similar(session: Session, item: Item, hours: int = 72) -> int:
    """Immediate effect of ignore: expire not-yet-digested items from the same source+author or same
    canonical domain with very similar title within the window. Conservative on purpose."""
    now = datetime.now(timezone.utc)
    cutoff = now - timedelta(hours=hours)
    # author identity only meaningful where the author *is* the content owner (github repo owner, x account)
    src_pairs = {(s.source, s.author_key) for s in item.sources
                 if s.author_key and s.source.split(":")[0] in ("github", "x", "wechat")}
    title_words = {w for w in (item.title or "").lower().split() if len(w) > 3}
    cands = session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.status.in_(("queued", "scored")), Item.first_seen_at > cutoff, Item.id != item.id)
    ).all()
    n = 0
    for c in cands:
        same_author = any((s.source, s.author_key) in src_pairs for s in c.sources)
        cw = {w for w in (c.title or "").lower().split() if len(w) > 3}
        jacc = len(cw & title_words) / max(len(cw | title_words), 1)
        if (same_author and jacc >= 0.3) or jacc >= 0.6:
            c.status = "expired"   # queued/scored → expired (allowed in TRANSITIONS)
            n += 1
    return n


def mute(session: Session, kind: str, key: str, days: int = 7) -> dict:
    p = session.get(Preference, (kind, key))
    if p is None:
        p = Preference(kind=kind, key=key)
        session.add(p)
    p.muted_until = datetime.now(timezone.utc) + timedelta(days=days)
    return {"kind": kind, "key": key, "muted_until": p.muted_until.isoformat()}


def list_inbox(session: Session, limit: int = 50) -> list[dict]:
    saved = session.execute(
        select(Feedback.item_id, Feedback.ts, Feedback.note).where(Feedback.action == "save")
        .order_by(Feedback.ts.desc()).limit(limit * 2)
    ).all()
    seen, out = set(), []
    for item_id, ts, note in saved:
        if item_id in seen:
            continue
        seen.add(item_id)
        it = session.get(Item, item_id)
        if not it:
            continue
        # unsave later than save?
        unsaved = session.scalar(select(Feedback.id).where(Feedback.item_id == item_id, Feedback.action == "unsave", Feedback.ts > ts))
        if unsaved:
            continue
        out.append({"id": it.id, "title": it.title, "url": it.url, "saved_at": ts.isoformat(), "note": note,
                    "summary_one": it.summary_one, "entities": it.entities_matched})
        if len(out) >= limit:
            break
    return out
