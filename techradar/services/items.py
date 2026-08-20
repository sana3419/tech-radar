from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import func, or_, select, text
from sqlalchemy.orm import Session, selectinload

from ..models import Feedback, Item


def item_to_dict(it: Item, full: bool = False) -> dict:
    d = {
        "id": it.id, "title": it.title, "url": it.url, "kind": it.kind, "status": it.status,
        "published_at": it.published_at.isoformat() if it.published_at else None,
        "first_seen_at": it.first_seen_at.isoformat(),
        "score": it.score, "reasons": it.reasons or [], "summary_one": it.summary_one,
        "summary_points": it.summary_points or [],
        "tags": it.tags, "entities": it.entities_matched,
        "sources": [{"source": s.source, "url": s.source_url, "author": s.author, "metrics": s.metrics_raw}
                    for s in it.sources],
    }
    if full:
        d.update({"score_breakdown": it.score_breakdown,
                  "content": (it.content or "")[:4000], "ranker_version": it.ranker_version})
    return d


def get_item(session: Session, item_id: int) -> dict | None:
    it = session.scalar(select(Item).options(selectinload(Item.sources)).where(Item.id == item_id))
    return item_to_dict(it, full=True) if it else None


def _terms(query: str) -> list[str]:
    """Split query into search terms: whitespace tokens; Chinese runs kept whole (trigram handles substrings)."""
    import re
    toks = [t for t in re.split(r"[\s,，、;；]+", query.strip()) if t]
    return toks[:6] or [query.strip()]


def search_items(session: Session, query: str, since: datetime | None = None, until: datetime | None = None,
                 only_saved: bool = False, limit: int = 20) -> list[dict]:
    """Hybrid lexical search: tsvector (english) for word matches + pg_trgm similarity (title/summary) for
    Chinese and substrings. Ranked by combined score. P1 adds pgvector."""
    terms = _terms(query)
    tsq = func.plainto_tsquery("english", query)
    tsv = func.to_tsvector("english", func.coalesce(Item.title, "") + " " + func.coalesce(Item.summary_one, ""))
    title = Item.title                     # NOT NULL → index-friendly
    summ = func.coalesce(Item.summary_one, "")
    content = func.left(func.coalesce(Item.content, ""), 2000)
    conds = [tsv.op("@@")(tsq)]
    sim_parts = []
    for t in terms:
        like = t.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        conds.append(title.ilike(f"%{like}%", escape="\\"))
        conds.append(summ.ilike(f"%{like}%", escape="\\"))
        # trigram: `<%` (word_similarity ≥ pg_trgm.word_similarity_threshold) can use the gin index;
        # ASCII short words are too noisy → only fuzzy-match ASCII terms of length ≥5, CJK ≥2
        if (t.isascii() and len(t) >= 5) or (not t.isascii() and len(t) >= 2):
            conds.append(title.op("%>")(t))
            conds.append(summ.op("%>")(t))
        sim_parts.append(func.greatest(func.word_similarity(t, title), func.word_similarity(t, summ)))
    whole = query.strip().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    conds.append(content.ilike(f"%{whole}%", escape="\\"))   # full phrase in body only
    sim = sum(sim_parts[1:], sim_parts[0]) / len(sim_parts) if sim_parts else 0
    rank = func.ts_rank(tsv, tsq) * 2 + sim
    q = (select(Item).options(selectinload(Item.sources)).where(or_(*conds))
         .where(Item.status != "filtered"))
    if since:
        q = q.where(Item.first_seen_at >= since)
    if until:
        q = q.where(Item.first_seen_at <= until)
    if only_saved:
        q = q.where(Item.id.in_(select(Feedback.item_id).where(Feedback.action == "save")))
    q = q.order_by(rank.desc(), Item.first_seen_at.desc()).limit(limit)
    return [item_to_dict(it) for it in session.scalars(q).all()]


def today_feed(session: Session, limit: int = 50) -> list[dict]:
    """Unread stream: scored/digested items without read/save/ignore feedback, by score."""
    # read/ignore hide permanently; save hides only while currently saved (latest save newer than latest unsave)
    acted = select(Feedback.item_id).where(Feedback.action.in_(("read", "ignore")))
    last_save = (select(Feedback.item_id, func.max(Feedback.ts).label("ts")).where(Feedback.action == "save")
                 .group_by(Feedback.item_id).subquery())
    last_unsave = (select(Feedback.item_id, func.max(Feedback.ts).label("ts")).where(Feedback.action == "unsave")
                   .group_by(Feedback.item_id).subquery())
    currently_saved = (select(last_save.c.item_id).outerjoin(last_unsave, last_unsave.c.item_id == last_save.c.item_id)
                       .where(or_(last_unsave.c.ts.is_(None), last_unsave.c.ts < last_save.c.ts)))
    q = (select(Item).options(selectinload(Item.sources))
         .where(Item.status.in_(("scored", "digested")), ~Item.id.in_(acted), ~Item.id.in_(currently_saved))
         .order_by(Item.score.desc().nullslast()).limit(limit))
    return [item_to_dict(it) for it in session.scalars(q).all()]
