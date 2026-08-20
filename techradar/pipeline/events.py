"""Soft event folding: canonical_key already hard-merges identical things; this groups *near-duplicate
titles across sources* (same story reposted) into one event within a 48h window.

Method (no LLM, no embedding): pure-Python pairwise token-jaccard on titles (window is small),
plus shared-entity relaxation; union into item.event_id (smallest item id in the group wins).
"""
from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import Item

log = logging.getLogger(__name__)
WINDOW_H = 48
SIM_THR = 0.55

_STOP = {"the", "a", "an", "for", "with", "and", "of", "to", "in", "on", "is", "how", "why", "what",
         "show", "hn", "ask", "via", "using", "your", "new"}


def _tokens(title: str) -> set[str]:
    toks = {t.lower() for t in re.findall(r"[A-Za-z0-9][A-Za-z0-9.+#-]{1,}", title or "")}
    return {t for t in toks if t not in _STOP and len(t) > 2}


def _similar_tok(ta: set, tb: set, a: Item, b: Item) -> bool:
    if not ta or not tb:
        return False
    jacc = len(ta & tb) / len(ta | tb)
    if jacc >= 0.5:
        return True
    # entity overlap + moderate token overlap
    ea, eb = set(a.entities_matched or []), set(b.entities_matched or [])
    return bool(ea & eb) and jacc >= 0.3


def _similar(a: Item, b: Item) -> bool:
    return _similar_tok(_tokens(a.title), _tokens(b.title), a, b)


def fold_events(session: Session, hours: int = WINDOW_H, now: datetime | None = None) -> int:
    """Assign event_id to near-duplicate items in the window. Returns #items newly grouped."""
    now = now or datetime.now(timezone.utc)
    since = now - timedelta(hours=hours)
    items = list(session.scalars(
        select(Item).where(Item.first_seen_at > since, Item.status.in_(("queued", "enriched", "scored", "digested")))
        .order_by(Item.id)
    ).all())
    n = 0
    # union-find over pairwise trigram-similar titles (N is small: window items only)
    parent: dict[int, int] = {}

    def find(x: int) -> int:
        while parent.get(x, x) != x:
            parent[x] = parent.get(parent[x], parent[x])
            x = parent[x]
        return x

    def union(x: int, y: int):
        rx, ry = find(x), find(y)
        if rx != ry:
            parent[max(rx, ry)] = min(rx, ry)

    for it in items:
        parent.setdefault(it.id, it.event_id or it.id)
    by_id = {it.id: it for it in items}
    toks = {it.id: _tokens(it.title) for it in items}
    for i, a in enumerate(items):
        for b in items[i + 1:]:
            if a.canonical_key == b.canonical_key:
                continue
            if _similar_tok(toks[a.id], toks[b.id], a, b):
                union(a.id, b.id)
    for it in items:
        root = find(it.id)
        if root != it.id or (it.event_id and it.event_id != root):
            if it.event_id != root:
                it.event_id = root
                n += 1
        elif it.event_id is None:
            pass   # singleton: leave NULL (event_id set only for grouped items)
    # ensure group roots also carry event_id when they have members
    roots_with_members = {find(i.id) for i in items if find(i.id) != i.id}
    for r in roots_with_members:
        it = by_id.get(r)
        if it is not None and it.event_id != r:
            it.event_id = r
            n += 1
    return n
