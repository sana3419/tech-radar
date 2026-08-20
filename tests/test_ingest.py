"""Ingest integration tests — need the dev Postgres (docker techradar-db). Everything runs inside a
transaction that is rolled back, so the dev DB is not polluted."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import func, select

from techradar.fetchers.base import FetchResult, RawItem
from techradar.models import Item, ItemSource, Snapshot, SourceHealth
from techradar.pipeline.ingest import ingest_raw, ingest_result


@pytest.fixture
def session():
    from techradar.db import get_engine
    from sqlalchemy.orm import Session
    try:
        eng = get_engine()
        conn = eng.connect()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"db unavailable: {e}")
    trans = conn.begin()
    s = Session(bind=conn, join_transaction_mode="create_savepoint")
    try:
        yield s
    finally:
        s.close()
        trans.rollback()
        conn.close()


NOW = datetime(2026, 8, 18, 12, 0, tzinfo=timezone.utc)
UNIQ = str(int(NOW.timestamp()))


def hn(url, oid="900000001", points=100):
    return RawItem(source="hackernews", external_id=oid, title="HN: repo", url=url,
                   source_url=f"https://news.ycombinator.com/item?id={oid}", kind="article",
                   published_at=NOW - timedelta(hours=1), metrics={"points": points, "comments": 1})


def gh(url, rid="999999999"):
    return RawItem(source="github", external_id=rid, title="o/r: desc", url=url, source_url=url, kind="repo",
                   author="o", author_key="o", published_at=NOW - timedelta(days=2), metrics={"stars": 120, "forks": 3})


def test_hn_and_github_merge_into_one_item(session):
    """docs/03 D3 acceptance: HN post linking a GitHub repo + github source -> 1 item, 2 item_sources."""
    url = f"https://github.com/test-owner-{UNIQ}/repo"
    i1, c1, s1 = ingest_raw(session, hn(url + "?utm_source=hn"), NOW)
    i2, c2, s2 = ingest_raw(session, gh(url + "/"), NOW)
    assert (c1, s1, c2, s2) == (True, True, False, True)
    assert i1.id == i2.id and i1.canonical_key == f"gh:test-owner-{UNIQ}/repo"
    srcs = session.scalars(select(ItemSource).where(ItemSource.item_id == i1.id)).all()
    assert sorted(s.source for s in srcs) == ["github", "hackernews"]
    assert i1.kind == "repo"                     # upgraded from HN's "article"
    assert i1.published_at == NOW - timedelta(days=2)   # earliest wins
    assert i1.published_at.tzinfo is not None
    assert i1.url == url                          # normalized, no utm / trailing slash


def test_hn_only_github_link_gets_repo_kind(session):
    item, _, _ = ingest_raw(session, hn(f"https://github.com/solo-owner-{UNIQ}/repo", oid="900000002"), NOW)
    assert item.kind == "repo"
    paper, _, _ = ingest_raw(session, hn("https://arxiv.org/abs/2408.99999", oid="900000003"), NOW)
    assert paper.kind == "paper"


def test_ingest_is_idempotent_and_snapshots_per_run(session):
    url = f"https://example.com/post-{UNIQ}"
    res = FetchResult(source="hackernews", items=[hn(url, oid="900000010"), hn(url, oid="900000010")], calls=1)
    st1 = ingest_result(session, res)
    assert st1.new_items == 1 and st1.new_sources == 1 and not st1.errors
    n_items = session.scalar(select(func.count()).select_from(Item))
    n_src = session.scalar(select(func.count()).select_from(ItemSource))
    # second run, later
    res2 = FetchResult(source="hackernews", items=[hn(url, oid="900000010", points=150)], calls=1)
    st2 = ingest_result(session, res2)
    assert st2.new_items == 0 and st2.new_sources == 0 and st2.merged_items == 1
    assert session.scalar(select(func.count()).select_from(Item)) == n_items
    assert session.scalar(select(func.count()).select_from(ItemSource)) == n_src
    src = session.scalar(select(ItemSource).where(ItemSource.external_id == "900000010"))
    assert src.metrics_raw == {"points": 150, "comments": 1}
    snaps = session.scalars(select(Snapshot).where(Snapshot.item_source_id == src.id)).all()
    assert len(snaps) >= 2 and all(s.ts.tzinfo is not None for s in snaps)
    item = session.get(Item, src.item_id)
    assert item.last_seen_at >= item.first_seen_at and item.status == "new"


def test_same_ts_snapshot_is_deduped(session):
    r = hn(f"https://example.com/snap-{UNIQ}", oid="900000020")
    ingest_raw(session, r, NOW)
    ingest_raw(session, r, NOW)                  # same `now` -> PK conflict -> do nothing
    src = session.scalar(select(ItemSource).where(ItemSource.external_id == "900000020"))
    assert session.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.item_source_id == src.id)) == 1


def test_no_snapshot_when_no_metrics(session):
    r = RawItem(source="rss:t", external_id=f"rss-{UNIQ}", title="t", url=f"https://example.com/rss-{UNIQ}", metrics={})
    ingest_raw(session, r, NOW)
    src = session.scalar(select(ItemSource).where(ItemSource.external_id == f"rss-{UNIQ}"))
    assert session.scalar(select(func.count()).select_from(Snapshot).where(Snapshot.item_source_id == src.id)) == 0


def test_bad_item_does_not_abort_batch(session):
    bad = RawItem(source="hackernews", external_id="900000030", title="x" * 10, url="https://example.com/ok-" + UNIQ)
    bad.title = None  # type: ignore[assignment]  # violates NOT NULL -> flush error inside savepoint
    good = hn(f"https://example.com/good-{UNIQ}", oid="900000031")
    st = ingest_result(session, FetchResult(source="hackernews", items=[bad, good], calls=1))
    assert len(st.errors) == 1 and st.new_items == 1
    h = session.get(SourceHealth, "hackernews")
    assert h is not None and h.last_items == 1 and h.consecutive_failures == 0


def test_health_failure_and_recovery(session):
    ingest_result(session, FetchResult(source="ztest", items=[], calls=2, error="boom"))
    h = session.get(SourceHealth, "ztest")
    assert h.consecutive_failures == 1 and h.last_error == "boom" and h.month_calls == 2 and h.last_success_at is None
    ingest_result(session, FetchResult(source="ztest", items=[], calls=1))
    assert h.consecutive_failures == 0 and h.last_error is None and h.month_calls == 3 and h.last_success_at is not None


def test_month_budget_persisted(session):
    from techradar.fetchers.hn import HackerNewsFetcher
    ingest_result(session, FetchResult(source="hackernews", items=[], calls=1))
    assert session.get(SourceHealth, "hackernews").month_budget == HackerNewsFetcher.month_budget
