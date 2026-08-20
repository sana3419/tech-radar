from datetime import datetime, timedelta, timezone

from techradar.fetchers.base import RawItem
from techradar.models import Item, ItemSource, Preference
from techradar.pipeline.heat import HeatModel
from techradar.pipeline.ingest import ingest_raw
from techradar.pipeline.matching import _query_matches, match_item
from techradar.pipeline.preferences import PrefModel, apply_feedback_to_prefs
from techradar.pipeline.score import score_item
from tests.test_ingest import session, UNIQ  # noqa: F401

NOW = datetime.now(timezone.utc)


def _item(title, sources=(("hackernews", {"points": 100}),), author=None, hours=1, tags=None, hints=None):
    it = Item(title=title, url="https://x", canonical_key="k", kind="article", status="queued",
              first_seen_at=NOW - timedelta(hours=hours), last_seen_at=NOW, published_at=NOW - timedelta(hours=hours))
    it.tags = tags
    it.sources = [ItemSource(source=s, external_id=str(i), metrics_raw=m, seen_at=NOW, author_key=author,
                             raw={"tags_hint": hints} if hints else {}) for i, (s, m) in enumerate(sources)]
    return it


def test_query_matches_word_boundary():
    assert _query_matches("vllm", "vllm 0.9 released")
    assert _query_matches("vllm", "vllm-project/vllm")
    assert not _query_matches("agent", "reagents in chemistry")
    assert _query_matches("llama.cpp", "one llama.cpp flag")
    assert _query_matches("local llm inference", "fast local llm inference on cpu")


def test_match_item_topics_entities_authors():
    it = _item("vLLM 0.9: MoE inference 2x", author="ggerganov", sources=(("github", {"stars": 10}),))
    h = match_item(it)
    assert any(t["name"] == "llm-inference" for t in h.topics)
    assert "vLLM" in h.entities
    assert h.authors and h.authors[0]["key"] == "ggerganov"


def test_heat_percentile_and_neutral():
    hm = HeatModel({"hackernews": sorted(__import__("math").log1p(v) for v in range(1, 101))})
    hot = _item("x", sources=(("hackernews", {"points": 99}),))
    cold = _item("x", sources=(("hackernews", {"points": 2}),))
    none = _item("x", sources=(("rss:foo", {}),))
    assert hm.item_heat(hot)[0] > 0.95 and hm.item_heat(cold)[0] < 0.1 and hm.item_heat(none)[0] == 0.5


def test_score_orders_sub_hit_above_heat_and_decays():
    hm = HeatModel({"hackernews": sorted(__import__("math").log1p(v) for v in range(1, 101))})
    prefs = PrefModel({}, {}, NOW)
    sub = _item("new vllm release", sources=(("hackernews", {"points": 5}),))
    hot = _item("unrelated but hot", sources=(("hackernews", {"points": 99}),))
    old_sub = _item("new vllm release", sources=(("hackernews", {"points": 5}),), hours=96)
    for it in (sub, hot, old_sub):
        score_item(it, hm, prefs, NOW)
    assert sub.score > hot.score > 0
    assert sub.score > old_sub.score
    assert "命中订阅" in " ".join(sub.reasons) and "Top 5%" in " ".join(hot.reasons)
    assert set(sub.score_breakdown) >= {"sub_hit", "heat_pct", "decay", "pref_mult", "hits", "weights"}


def test_cross_source_bonus():
    hm = HeatModel({})
    prefs = PrefModel({}, {}, NOW)
    one = _item("t", sources=(("hackernews", {"points": 5}),))
    two = _item("t", sources=(("hackernews", {"points": 5}), ("github", {"stars": 5})))
    score_item(one, hm, prefs, NOW); score_item(two, hm, prefs, NOW)
    assert two.score > one.score and "2 个来源同时出现" in two.reasons


def test_pref_multiplier_and_mute():
    p_bad = Preference(kind="source", key="hackernews", alpha=1, beta=9)
    p_mute = Preference(kind="source", key="rss:junk", alpha=1, beta=1, muted_until=NOW + timedelta(days=7))
    pm = PrefModel({("source", "hackernews"): p_bad, ("source", "rss:junk"): p_mute}, {"stacks": {"rust": 1.3}}, NOW)
    it = _item("t", sources=(("hackernews", {"points": 5}),), hints=["rust"])
    m, d = pm.item_multiplier(it)
    assert 0.5 <= m < 1.0 and d.get("hint:rust") == 1.3
    muted = _item("t", sources=(("rss:junk", {}),))
    assert pm.item_multiplier(muted)[0] == 0.0


def test_feedback_updates_prefs(session):
    it, _, _ = ingest_raw(session, RawItem(source="hackernews", external_id=f"93{UNIQ[:6]}", title="t",
                                          url=f"https://e.com/p-{UNIQ}", author_key="bob", metrics={"points": 1}), NOW, expand_short=False)
    session.refresh(it)
    apply_feedback_to_prefs(session, it, "ignore")
    session.flush()
    p = session.get(Preference, ("author", "hackernews:bob"))
    assert p and p.beta == 2.0 and p.alpha == 1.0
    apply_feedback_to_prefs(session, it, "save")
    session.flush()
    assert p.alpha == 2.0
