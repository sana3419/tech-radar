"""Entity briefs, topic MOCs, saved notes, backlinks and orphan pruning."""
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from techradar.models import Entity, EntityTimeline, Item, ItemSource
from tests.test_ingest import UNIQ, session  # noqa: F401

NOW = datetime.now(timezone.utc)


def _item(session, title, ents=None, key="k", score=999.0):
    """score defaults high so fixtures outrank real rows in the dev DB's top-N queries."""
    it = Item(canonical_key=f"kn:{UNIQ}:{key}", kind="article", title=title,
              url=f"https://e.com/{UNIQ}/{key}", first_seen_at=NOW - timedelta(hours=1),
              last_seen_at=NOW, status="scored", score=score, entities_matched=ents,
              summary_one=f"{title} 的中文摘要",
              score_breakdown={"hits": {"topics": [{"name": "llm-inference", "label": "推理框架",
                                                    "boost": 1.5, "query": "vllm"}]}, "sub_hit": 1.5})
    session.add(it)
    session.flush()
    session.add(ItemSource(item_id=it.id, source="hackernews", external_id=f"{UNIQ}-{key}",
                           source_url=it.url, seen_at=it.first_seen_at))
    session.flush()
    return it


def _entity(session, name):
    e = Entity(canonical_name=name, type="project", first_seen_at=NOW, anchors={"github": "o/r"})
    session.add(e)
    session.flush()
    return e


# ---------- entity brief ----------
def test_needs_refresh_tracks_timeline_growth(session):
    from techradar.agents.brief import needs_refresh
    e = _entity(session, f"BriefEnt{UNIQ}")
    assert needs_refresh(session, e) is False          # no timeline yet → nothing to summarize
    it = _item(session, "brief source", ents=[e.canonical_name], key="b1")
    session.add(EntityTimeline(entity_id=e.id, item_id=it.id, event_type="mention", ts=NOW))
    session.flush()
    assert needs_refresh(session, e) is True           # timeline appeared
    from techradar.agents.brief import PROMPT_VERSION, _fingerprint, _fp_key
    e.brief = {"status": "x", "_v": PROMPT_VERSION}
    e.brief_source_count = _fp_key(*_fingerprint(session, e.id))
    session.flush()
    assert needs_refresh(session, e) is False          # unchanged → skip (costs nothing)
    it2 = _item(session, "brief source 2", ents=[e.canonical_name], key="b2")
    session.add(EntityTimeline(entity_id=e.id, item_id=it2.id, event_type="release", ts=NOW))
    session.flush()
    assert needs_refresh(session, e) is True           # grew → refresh


def test_refresh_entity_writes_card(session, monkeypatch):
    from techradar.agents import brief
    from techradar.llm.schemas import EntityBriefOut
    e = _entity(session, f"CardEnt{UNIQ}")
    it = _item(session, "card source", ents=[e.canonical_name], key="c1")
    session.add(EntityTimeline(entity_id=e.id, item_id=it.id, event_type="release", ts=NOW))
    session.flush()
    out = EntityBriefOut(status="活跃开发", activity="发了新版本", trend="升温：本周 3 条",
                         advice="值得试", highlights=["h1"])
    monkeypatch.setattr(brief, "structured", lambda *a, **k: (out, {"model": "test-model"}))
    assert brief.refresh_entity(session, e) is True
    assert e.brief["status"] == "活跃开发" and e.brief_model == "test-model"
    assert e.brief["_v"] == brief.PROMPT_VERSION       # prompt version stamped for later invalidation
    assert e.brief_source_count == brief._fp_key(*brief._fingerprint(session, e.id))


def test_brief_skips_when_llm_unconfigured(session, monkeypatch):
    from techradar.agents import brief
    monkeypatch.setattr(brief, "is_configured", lambda: False)
    st = brief.refresh_all(session)
    assert st["updated"] == 0 and st["errors"]


# ---------- topic MOC ----------
def test_topic_items_matches_subscription_hits(session):
    from techradar.agents.moc import topic_items
    it = _item(session, "moc source", key="m1")
    ids = {x.id for x in topic_items(session, "llm-inference")}
    assert it.id in ids
    assert it.id not in {x.id for x in topic_items(session, "no-such-topic")}


def test_build_moc_without_llm_still_lists_items(session, monkeypatch):
    from techradar.agents import moc
    monkeypatch.setattr(moc, "is_configured", lambda: False)
    it = _item(session, "moc listed", key="m2")
    topic = SimpleNamespace(name="llm-inference", label="推理框架", queries=["vllm"])
    out = moc.build_moc(session, topic, [it])
    assert out["narrative"] is None and out["items"][0]["id"] == it.id


# ---------- notes ----------
def test_save_answer_writes_note_with_links(session, tmp_path, monkeypatch):
    from techradar.render import notes as notes_mod
    monkeypatch.setattr(notes_mod, "vault_dir", lambda: tmp_path)
    it = _item(session, "cited item", ents=["vLLM"], key="n1")
    p = notes_mod.save_answer(session, "vLLM 有什么新版本？", "答案正文 [1]",
                              [{"n": 1, "id": it.id, "title": it.title, "url": it.url}])
    text = p.read_text(encoding="utf-8")
    assert "entities: [vLLM]" in text
    assert "[[entities/vLLM|vLLM]]" in text
    assert "## 我的笔记" in text                      # space reserved for the human
    p2 = notes_mod.save_answer(session, "vLLM 有什么新版本？", "第二次", [])
    assert p2 != p                                    # never clobbers an edited note


# ---------- pruning ----------
def test_prune_removes_orphans_but_not_handwritten(session, tmp_path, monkeypatch):
    from techradar.render import obsidian
    monkeypatch.setattr(obsidian, "vault_dir", lambda: tmp_path)
    (tmp_path / "digests").mkdir(parents=True)
    orphan = tmp_path / "digests" / "1999-01-01.md"
    orphan.write_text("---\ngenerated: techradar\n---\nold", encoding="utf-8")
    mine = tmp_path / "digests" / "my-own-notes.md"
    mine.write_text("# 手写的，不该被删", encoding="utf-8")
    removed = obsidian.prune_orphans(session)
    assert "digests/1999-01-01.md" in removed
    assert not orphan.exists() and mine.exists()


def test_brief_invalidated_by_prompt_version(session):
    """Changing the prompt must invalidate cards written by the old one."""
    from techradar.agents.brief import _fingerprint, _fp_key, needs_refresh
    e = _entity(session, f"VerEnt{UNIQ}")
    it = _item(session, "ver source", ents=[e.canonical_name], key="v1")
    session.add(EntityTimeline(entity_id=e.id, item_id=it.id, event_type="mention", ts=NOW))
    session.flush()
    e.brief = {"status": "old", "_v": "brief-v0"}
    e.brief_source_count = _fp_key(*_fingerprint(session, e.id))
    session.flush()
    assert needs_refresh(session, e) is True


def test_needs_refresh_detects_delete_then_readd(session):
    """Same row count, different rows — a plain COUNT would miss this."""
    from techradar.agents.brief import PROMPT_VERSION, _fingerprint, _fp_key, needs_refresh
    e = _entity(session, f"SwapEnt{UNIQ}")
    a = _item(session, "swap a", ents=[e.canonical_name], key="s1")
    tl = EntityTimeline(entity_id=e.id, item_id=a.id, event_type="mention", ts=NOW)
    session.add(tl)
    session.flush()
    e.brief = {"status": "x", "_v": PROMPT_VERSION}
    e.brief_source_count = _fp_key(*_fingerprint(session, e.id))
    session.flush()
    assert needs_refresh(session, e) is False
    session.delete(tl)
    b = _item(session, "swap b", ents=[e.canonical_name], key="s2")
    session.add(EntityTimeline(entity_id=e.id, item_id=b.id, event_type="mention", ts=NOW))
    session.flush()
    assert needs_refresh(session, e) is True           # count unchanged, contents changed
