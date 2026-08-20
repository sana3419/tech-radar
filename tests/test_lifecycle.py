from datetime import datetime, timedelta, timezone

import pytest

from techradar.fetchers.base import RawItem
from techradar.models import Feedback, Item
from techradar.pipeline.ingest import ingest_raw
from techradar.pipeline.lifecycle import expire_unread, transition
from techradar.pipeline.snapshot import due_item_sources

# reuse the DB fixture from test_ingest
from tests.test_ingest import session, UNIQ  # noqa: F401


def _mk(session, url, oid, ago_hours=0, metrics=None):
    now = datetime.now(timezone.utc) - timedelta(hours=ago_hours)
    item, _, _ = ingest_raw(session, RawItem(source="hackernews", external_id=oid, title="t", url=url,
                                              metrics=metrics or {"points": 1}), now, expand_short=False)
    return item


def test_transitions():
    it = Item(status="new")
    transition(None, it, "queued"); assert it.status == "queued"
    transition(None, it, "enriched"); transition(None, it, "scored"); transition(None, it, "digested")
    with pytest.raises(ValueError):
        transition(None, it, "queued")


def test_expire_only_unread_old_scored(session):
    old = _mk(session, f"https://e.com/old-{UNIQ}", f"91{UNIQ[:6]}1", ago_hours=72); old.status = "scored"
    saved = _mk(session, f"https://e.com/saved-{UNIQ}", f"91{UNIQ[:6]}2", ago_hours=72); saved.status = "digested"
    session.add(Feedback(item_id=saved.id, action="save"))
    fresh = _mk(session, f"https://e.com/fresh-{UNIQ}", f"91{UNIQ[:6]}3", ago_hours=1); fresh.status = "scored"
    new = _mk(session, f"https://e.com/new-{UNIQ}", f"91{UNIQ[:6]}4", ago_hours=100)  # status new: never expires here
    session.flush()
    n = expire_unread(session, hours=48)
    session.flush()
    for o in (old, saved, fresh, new):
        session.refresh(o)
    assert old.status == "expired"
    assert saved.status == "digested" and fresh.status == "scored" and new.status == "new"
    assert n >= 1


def test_due_snapshots_tiers(session):
    now = datetime.now(timezone.utc)
    hot = _mk(session, f"https://e.com/hot-{UNIQ}", f"92{UNIQ[:6]}1", ago_hours=2)     # snapshot 2h ago → due
    warm = _mk(session, f"https://e.com/warm-{UNIQ}", f"92{UNIQ[:6]}2", ago_hours=48)  # snapshot 48h ago → due (6h tier)
    cold = _mk(session, f"https://e.com/cold-{UNIQ}", f"92{UNIQ[:6]}3", ago_hours=24 * 9)  # >7d → never
    session.flush()
    due_ids = {s.item_id for s in due_item_sources(session, now, limit=1000)}
    assert hot.id in due_ids and warm.id in due_ids and cold.id not in due_ids
