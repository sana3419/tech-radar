"""W3 acceptance tests: event folding / weekly / tuning / obsidian. DB tests run inside a
rolled-back transaction against the dev Postgres (same pattern as test_ingest.py)."""
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from techradar.models import Entity, EntityTimeline, Item, ItemSource
from techradar.pipeline.events import _similar, _tokens, fold_events

NOW = datetime(2026, 8, 19, 12, 0, tzinfo=timezone.utc)
UNIQ = str(int(NOW.timestamp()))


@pytest.fixture
def session():
    from sqlalchemy.orm import Session

    from techradar.db import get_engine
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


def _mk(session, title, src, key, ents=None, score=1.0, status="scored"):
    it = Item(canonical_key=f"test:{UNIQ}:{key}", kind="article", title=title,
              url=f"https://example.com/{UNIQ}/{key}", first_seen_at=NOW - timedelta(hours=2),
              last_seen_at=NOW, status=status, score=score, entities_matched=ents,
              score_breakdown={"hits": {"topics": [], "authors": [], "entities": ents or []},
                               "sub_hit": 0})
    session.add(it)
    session.flush()
    session.add(ItemSource(item_id=it.id, source=src, external_id=f"{UNIQ}-{key}",
                           source_url=it.url, seen_at=it.first_seen_at))
    session.flush()
    return it


# ---------- 1. event folding ----------

def test_tokens_stopwords_and_short():
    t = _tokens("Show HN: How to use vLLM for fast LLM inference v0.27.1")
    assert "show" not in t and "hn" not in t and "to" not in t
    assert "vllm" in t and "v0.27.1" in t


def test_similar_entity_path():
    a = Item(title="vLLM v0.27.1", entities_matched=["vLLM"], canonical_key="a")
    b = Item(title="vLLM v0.27.0", entities_matched=["vLLM"], canonical_key="b")
    assert _similar(a, b)  # jaccard <0.5 but shared entity + >=0.3
    c = Item(title="totally unrelated rust kernel post", entities_matched=["vLLM"], canonical_key="c")
    assert not _similar(a, c)


def test_fold_groups_and_is_idempotent(session):
    a = _mk(session, f"SuperWidget {UNIQ} launches quantum inference engine", "hackernews", "e1")
    b = _mk(session, f"SuperWidget {UNIQ} launches quantum inference engine on GPUs", "rss:v2ex_tech", "e2")
    _mk(session, f"Unrelated kernel scheduler deep dive {UNIQ}", "github", "e3")
    n1 = fold_events(session, now=NOW)
    session.flush()
    assert n1 >= 2                      # both members got event_id
    assert a.event_id == a.id == b.event_id   # root = smallest id
    n2 = fold_events(session, now=NOW)  # idempotent: second run changes nothing
    assert n2 == 0


def test_fold_singleton_stays_null(session):
    x = _mk(session, f"Lonely one-off post {UNIQ} zzz", "hackernews", "s1")
    fold_events(session, now=NOW)
    session.flush()
    assert x.event_id is None


# ---------- 2. weekly ----------

def test_weekly_rising_requires_two_source_families(session):
    from techradar.digest.weekly import build_weekly, render_weekly
    a = _mk(session, f"CrossFam story {UNIQ} alpha beta gamma", "hackernews", "w1", score=99.0)
    b = _mk(session, f"CrossFam story {UNIQ} alpha beta gamma delta", "rss:v2ex_tech", "w2", score=98.0)
    solo = _mk(session, f"SoloFam story {UNIQ} epsilon zeta", "github", "w3", score=97.0)
    fold_events(session, now=NOW)
    session.flush()
    w = build_weekly(session)
    ids = {it.id for it in w["rising"]}
    assert a.id in ids or b.id in ids   # event group spans HN+RSS families
    assert solo.id not in ids           # single family excluded
    out = render_weekly(w)
    assert "本周回顾" in out and "📊" in out


def test_week_range_utc_construction():
    from datetime import date

    from techradar.digest.weekly import _week_range
    since, until = _week_range(date(2026, 8, 19))
    assert until - since == timedelta(days=7)
    assert until.tzinfo is timezone.utc
    # boundaries are local (configured tz) midnights converted to UTC
    from zoneinfo import ZoneInfo

    from techradar.settings import get_settings
    tz = ZoneInfo(get_settings().timezone)
    assert until == datetime(2026, 8, 20, tzinfo=tz).astimezone(timezone.utc)


# ---------- 3. tuning ----------

def test_topic_stats_tolerates_null_breakdown_and_lists_zero_hit(session):
    from techradar.services.tuning import muted, source_stats, topic_stats
    from techradar.settings import get_subscriptions
    it = _mk(session, f"No breakdown item {UNIQ}", "hackernews", "t1")
    it.score_breakdown = None           # must not break the JSONB lateral join
    session.flush()
    rows = topic_stats(session)
    names = {r["topic"] for r in rows}
    for t in get_subscriptions().topics:   # zero-hit topics are back-filled
        assert t.name in names
    srcs = source_stats(session)
    assert any(r["source"] == "hackernews" for r in srcs)
    muted(session)                      # smoke: no exception


def test_mute_service_roundtrip(session):
    from techradar.services.feedback import mute
    r = mute(session, "source", f"test-src-{UNIQ}", days=3)
    session.flush()
    from techradar.services.tuning import muted
    rows = muted(session)
    assert any(m["key"] == f"test-src-{UNIQ}" and m["active"] for m in rows)
    assert r["muted_until"] > datetime.now(timezone.utc).isoformat()[:10]


# ---------- 4. obsidian ----------

def test_safe_filename():
    from techradar.render.obsidian import _safe
    assert "/" not in _safe("a/b:c*d?e")
    assert _safe("a/b:c") == "a-b-c"
    assert _safe("///") == "unnamed"


def test_write_if_changed_idempotent(tmp_path):
    from techradar.render.obsidian import _write_if_changed
    p = tmp_path / "x.md"
    content = "---\ngenerated_hash: {{HASH}}\n---\nbody\n"
    assert _write_if_changed(p, content) is True
    assert _write_if_changed(p, content) is False          # unchanged → skip
    assert _write_if_changed(p, content + "more\n") is True  # changed → rewrite


def test_render_entity_wikilinks_are_safe(session):
    from techradar.render.obsidian import render_entity
    e = Entity(canonical_name=f"Weird/Name:{UNIQ}", type="tool",
               first_seen_at=NOW, watched=True)
    session.add(e)
    session.flush()
    it = _mk(session, f"weird entity item {UNIQ}", "hackernews", "o1",
             ents=[f"Weird/Name:{UNIQ}", "Other/Ent"])
    session.add(EntityTimeline(entity_id=e.id, item_id=it.id, event_type="mention", ts=NOW))
    session.flush()
    md = render_entity(session, e)
    assert "[[Other-Ent]]" in md        # wikilinks use _safe names
    assert "generated_hash: {{HASH}}" in md


def test_update_timeline_rowcount_and_idempotent(session):
    from techradar.services.entities import update_timeline
    e = Entity(canonical_name=f"TLEnt{UNIQ}", type="tool", first_seen_at=NOW)
    session.add(e)
    session.flush()
    _mk(session, f"timeline item {UNIQ}", "hackernews", "tl1", ents=[f"TLEnt{UNIQ}"])
    n1 = update_timeline(session)
    rows = session.scalars(select(EntityTimeline).where(EntityTimeline.entity_id == e.id)).all()
    assert len(rows) == 1              # insert happened exactly once
    n2 = update_timeline(session)
    rows = session.scalars(select(EntityTimeline).where(EntityTimeline.entity_id == e.id)).all()
    assert len(rows) == 1              # idempotent: conflict skipped
    assert n2 == 0                     # idempotent rerun inserts nothing
    # n1 counts *every* item→entity link inserted in this pass (other registry entities such as
    # vLLM/llama.cpp may match fixtures too), so only its lower bound is meaningful.
    assert n1 >= 1
    # Deterministic counting would need .returning(EntityTimeline.id).
