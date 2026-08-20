"""Round-2 acceptance probes. xfail = defect found in review (see report), not a regression."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from techradar.fetchers.base import RawItem
from techradar.models import Feedback, ItemSource
from techradar.pipeline.ingest import ingest_raw
from techradar.pipeline.snapshot import due_item_sources

from tests.test_ingest import session, UNIQ  # noqa: F401

NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)


def _raw(src, oid, url, **kw):
    return RawItem(source=src, external_id=oid, title="t", url=url, metrics=kw.pop("metrics", {"points": 1}), **kw)


def test_repoint_creates_orphan_with_feedback(session):
    """Same (source, external_id) re-seen with a different URL re-points item_source; old item is left
    orphaned together with its feedback (gc would delete a *saved* item)."""
    i1, _, _ = ingest_raw(session, _raw("hackernews", f"77{UNIQ[:7]}1", f"https://ex.com/A-{UNIQ}"), NOW, expand_short=False)
    session.add(Feedback(item_id=i1.id, action="save")); session.flush()
    i2, _, _ = ingest_raw(session, _raw("hackernews", f"77{UNIQ[:7]}1", f"https://ex.com/B-{UNIQ}"), NOW, expand_short=False)
    assert i2.id != i1.id
    assert session.scalar(select(func.count()).where(ItemSource.item_id == i1.id)) == 0   # orphan (gc-able)
    assert session.scalar(select(func.count()).where(Feedback.item_id == i1.id)) == 0     # feedback migrated
    assert session.scalar(select(func.count()).where(Feedback.item_id == i2.id)) == 1



def test_generic_kind_is_stable(session):
    url = f"https://ex.com/K-{UNIQ}"
    ingest_raw(session, _raw("rss:x", f"k-{UNIQ}", url, kind="article", metrics={}), NOW, expand_short=False)
    b, _, _ = ingest_raw(session, _raw("hackernews", f"77{UNIQ[:7]}2", url, kind="post"), NOW, expand_short=False)
    assert b.kind == "article"



def test_due_limit_is_fair_across_sources(session):
    now = datetime.now(timezone.utc)
    for i in range(5):
        ingest_raw(session, _raw("github", f"78{UNIQ[:6]}{i}", f"https://github.com/o{UNIQ}/r{i}", kind="repo",
                                 metrics={"stars": 1}), now - timedelta(hours=3), expand_short=False)
    for i in range(5):
        ingest_raw(session, _raw("hackernews", f"79{UNIQ[:6]}{i}", f"https://ex.com/h{UNIQ}-{i}"),
                   now - timedelta(hours=2), expand_short=False)
    due = due_item_sources(session, now, limit=5)
    assert {d.source for d in due} == {"github", "hackernews"}
