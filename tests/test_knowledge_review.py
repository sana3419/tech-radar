"""Review-round characterization tests for the knowledge layer (briefs / MOCs / ask / vault pruning).

Tests marked xfail encode the *desired* behaviour of a defect found in review: they stay green
today and flip to XPASS the moment the defect is fixed.
"""
from datetime import datetime, timedelta, timezone

import pytest

from techradar.models import Entity, EntityTimeline, Item
from tests.test_ingest import UNIQ, session  # noqa: F401

NOW = datetime.now(timezone.utc)
HI = 1e9  # outrank real dev-DB rows in top-N queries


def _item(session, key, topics, score=HI):
    it = Item(canonical_key=f"kr:{UNIQ}:{key}", kind="article", title=f"kr {key}",
              url=f"https://kr.example/{UNIQ}/{key}", first_seen_at=NOW - timedelta(hours=1),
              last_seen_at=NOW, status="scored", score=score, summary_one="摘要",
              score_breakdown={"hits": {"topics": topics}})
    session.add(it)
    session.flush()
    return it


# ---------- 1. entity brief: cache key ----------
@pytest.mark.xfail(reason="needs_refresh compares only COUNT(timeline); a purge+re-add keeps the "
                          "count while the content changes, so the card silently goes stale",
                   strict=False)
def test_needs_refresh_detects_replaced_timeline_rows(session):
    from techradar.agents.brief import needs_refresh
    e = Entity(canonical_name=f"KR{UNIQ}", type="project", first_seen_at=NOW, status="active")
    session.add(e)
    session.flush()
    a, b = _item(session, "t1", []), _item(session, "t2", [])
    old = EntityTimeline(entity_id=e.id, item_id=a.id, event_type="mention", ts=NOW)
    session.add(old)
    session.flush()
    e.brief, e.brief_source_count = {"status": "旧卡"}, 1
    session.flush()
    assert needs_refresh(session, e) is False           # unchanged → skip, as designed

    session.delete(old)                                 # cli.py purge / cascade from items
    session.add(EntityTimeline(entity_id=e.id, item_id=b.id, event_type="release", ts=NOW))
    session.flush()
    assert needs_refresh(session, e) is True            # different rows, same count


# ---------- 1b. entity brief: transaction hygiene ----------
@pytest.mark.xfail(reason="refresh_all rolls back the shared session on a per-entity failure, "
                          "discarding uncommitted work the caller did earlier (job_obsidian runs "
                          "sync_entities/update_timeline in the very same session_scope)",
                   strict=False)
def test_refresh_all_failure_keeps_callers_uncommitted_work(session, monkeypatch):
    from sqlalchemy import func, select

    from techradar.agents import brief
    name = f"KRNEW{UNIQ}"
    session.add(Entity(canonical_name=name, type="project", first_seen_at=NOW, status="active"))
    session.flush()                                     # like sync_entities: flushed, not committed

    def boom(*a, **k):
        raise RuntimeError("structured output failed after retry")

    monkeypatch.setattr(brief, "is_configured", lambda: True)
    monkeypatch.setattr(brief, "structured", boom)
    monkeypatch.setattr(brief, "needs_refresh", lambda s, e: e.canonical_name != name)
    brief.refresh_all(session, limit=1)
    assert session.scalar(select(func.count()).select_from(Entity)
                          .where(Entity.canonical_name == name)) == 1


# ---------- 2. topic MOC: JSONB predicate ----------
@pytest.mark.xfail(reason="topic_items LIKEs the whole topics array as text, so a topic whose name "
                          "equals another topic's query/label — or a JSON key — matches too; "
                          "use JSONB containment @> [{'name': topic}] instead",
                   strict=False)
def test_topic_items_does_not_match_other_json_fields(session):
    from techradar.agents.moc import topic_items
    decoy = _item(session, "decoy", [{"name": "other", "label": "L", "boost": 1.0, "query": "agents"}])
    keyish = _item(session, "keyish", [{"name": "zzz", "label": "L", "boost": 1.0, "query": "q"}])
    assert decoy.id not in {x.id for x in topic_items(session, "agents")}
    assert keyish.id not in {x.id for x in topic_items(session, "name")}


def test_topic_items_matches_exact_topic_name(session):
    """Guard the behaviour a containment-based rewrite must preserve."""
    from techradar.agents.moc import topic_items
    it = _item(session, "real", [{"name": f"kr-topic-{UNIQ}", "label": "标签", "boost": 1.0, "query": "q"}])
    assert it.id in {x.id for x in topic_items(session, f"kr-topic-{UNIQ}")}


# ---------- 3. MOC render: notable parsing ----------
@pytest.mark.parametrize("note", ["#7 理由", "7 理由"])
def test_render_moc_links_notable(note):
    from techradar.render.obsidian import render_moc
    md = render_moc({"topic": "t", "label": "推理框架", "queries": ["vllm"], "count": 1,
                     "items": [{"id": 7, "title": "T", "summary": "S", "url": "https://u",
                                "kind": "article", "date": "2026-08-19", "entities": ["vLLM"]}],
                     "narrative": {"summary": "s", "themes": ["a"], "notable": [note]}})
    assert "[S](https://u) — 理由" in md


@pytest.mark.xfail(reason="the #id regex is anchored with re.match, so common LLM decorations "
                          "('- #7 …', '**#7** …', '条目 #7 …') fall through to raw text with no link",
                   strict=False)
@pytest.mark.parametrize("note", ["- #7 理由", "**#7** 理由", "条目 #7 理由"])
def test_render_moc_tolerates_decorated_notable(note):
    from techradar.render.obsidian import render_moc
    md = render_moc({"topic": "t", "label": "推理框架", "queries": [], "count": 1,
                     "items": [{"id": 7, "title": "T", "summary": "S", "url": "https://u",
                                "kind": "article", "date": "2026-08-19", "entities": []}],
                     "narrative": {"summary": "s", "themes": [], "notable": [note]}})
    assert "- [S](https://u) — 理由" in md


# ---------- 4. prune_orphans safety ----------
def test_prune_skips_topics_when_mocs_missing(session, tmp_path, monkeypatch):
    """mocs=None (no MOC pass ran) must never touch topics/."""
    from techradar.render import obsidian
    monkeypatch.setattr(obsidian, "vault_dir", lambda: tmp_path)
    (tmp_path / "topics").mkdir(parents=True)
    p = tmp_path / "topics" / "旧主题.md"
    p.write_text("---\ntopic: old\ngenerated: techradar\n---\n", encoding="utf-8")
    assert obsidian.prune_orphans(session, mocs=None) == []
    assert p.exists()


@pytest.mark.xfail(reason="build_all breaks out of the loop on BudgetExceeded, so a truncated mocs "
                          "list makes prune_orphans delete the topic pages that were never rebuilt",
                   strict=False)
def test_prune_keeps_topics_missing_from_a_truncated_moc_run(session, tmp_path, monkeypatch):
    from techradar.render import obsidian
    monkeypatch.setattr(obsidian, "vault_dir", lambda: tmp_path)
    (tmp_path / "topics").mkdir(parents=True)
    still_subscribed = tmp_path / "topics" / "Rust 基础设施.md"
    still_subscribed.write_text("---\ntopic: rust-infra\ngenerated: techradar\n---\n", encoding="utf-8")
    obsidian.prune_orphans(session, mocs=[{"label": "推理框架"}])   # budget died after topic #1
    assert still_subscribed.exists()


@pytest.mark.xfail(reason="the marker is matched anywhere in the first 400 chars, so a hand-written "
                          "vault note that merely quotes 'generated: techradar' is deleted; "
                          "the check must be scoped to the YAML frontmatter block",
                   strict=False)
def test_prune_keeps_handwritten_note_mentioning_the_marker(session, tmp_path, monkeypatch):
    from techradar.render import obsidian
    monkeypatch.setattr(obsidian, "vault_dir", lambda: tmp_path)
    (tmp_path / "entities").mkdir(parents=True)
    mine = tmp_path / "entities" / "我的 vLLM 心得.md"
    mine.write_text("---\ntags: [mine]\n---\n\n生成页 frontmatter 里带 generated: techradar，别手改。\n",
                    encoding="utf-8")
    obsidian.prune_orphans(session)
    assert mine.exists()


def test_prune_bails_out_when_keep_set_is_empty(session, tmp_path, monkeypatch):
    """A wrong/empty DB must not wipe a populated vault directory."""
    from sqlalchemy import delete

    from techradar.models import Digest
    from techradar.render import obsidian
    monkeypatch.setattr(obsidian, "vault_dir", lambda: tmp_path)
    (tmp_path / "digests").mkdir(parents=True)
    for day in ("2026-08-18", "2026-08-19", "2026-08-20"):
        (tmp_path / "digests" / f"{day}.md").write_text(
            "---\ngenerated: techradar\n---\n", encoding="utf-8")
    session.execute(delete(Digest))
    session.flush()
    removed = obsidian.prune_orphans(session)
    # fixed: an empty keep-set means "we know of nothing", so the populated folder is left alone
    assert removed == []
    assert len(list((tmp_path / "digests").glob("*.md"))) == 3


# ---------- 4b. backlink loop ----------
def test_research_report_frontmatter_closes_the_backlink_loop(session, tmp_path, monkeypatch):
    from techradar.agents import research as R
    from techradar.render import obsidian
    monkeypatch.setattr(obsidian, "vault_dir", lambda: tmp_path)
    it = _item(session, "res", [])
    it.entities_matched = ["vLLM"]
    session.flush()
    out = R.ResearchOut(tldr="t", should_follow="是", key_facts=[], relation_to_known=[],
                        risks=[], sources=["https://s"])
    monkeypatch.setattr(R, "ROOT", tmp_path / "root")
    R._write_markdown(it, out, "正文")
    body = (tmp_path / "research").glob("*.md").__next__().read_text(encoding="utf-8")
    assert "entities: [vLLM]" in body
    assert obsidian._related_pages("vLLM")["research"]        # entity page gets the backlink
