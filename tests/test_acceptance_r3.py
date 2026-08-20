"""R3 acceptance: LLM structured path (mocked SDK), enrich batch handling, digest limits,
Telegram digest split/keyboard, feedback/hide_similar behaviour, pref multiplier edge cases."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from techradar.models import Item, ItemSource, LlmUsage, Preference
from techradar.pipeline.heat import HeatModel
from techradar.pipeline.preferences import PrefModel
from techradar.pipeline.score import score_item
from tests.test_ingest import session  # noqa: F401

NOW = datetime.now(timezone.utc)


def _item(title, sources=(("hackernews", {"points": 100}),), author=None, hours=1, status="queued", key=None):
    it = Item(title=title, url=f"https://x/{title}", canonical_key=key or f"k:{title}", kind="article", status=status,
              first_seen_at=NOW - timedelta(hours=hours), last_seen_at=NOW, published_at=NOW - timedelta(hours=hours))
    it.sources = [ItemSource(source=s, external_id=f"{key or title}-{i}", metrics_raw=m, seen_at=NOW, author_key=author, raw={})
                  for i, (s, m) in enumerate(sources)]
    return it


# ---------------- llm/client ----------------
class _FakeUsage:
    input_tokens = 1000
    output_tokens = 200
    cache_read_input_tokens = 500
    cache_creation_input_tokens = 0


def _fake_anthropic(monkeypatch, parsed, stop_reason="end_turn", capture: dict | None = None):
    import anthropic
    from techradar.settings import get_settings
    monkeypatch.setattr(get_settings(), "llm_provider", "anthropic")
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")

    class _Messages:
        def parse(self, **kw):
            if capture is not None:
                capture.update(kw)
            return SimpleNamespace(usage=_FakeUsage(), stop_reason=stop_reason, parsed_output=parsed)

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    monkeypatch.setattr(anthropic, "Anthropic", _Client)


def test_structured_parses_and_records_usage(session, monkeypatch):
    from techradar.llm import client
    from techradar.llm.schemas import EnrichBatchOut, EnrichOut
    out = EnrichBatchOut(items=[EnrichOut(summary_one="s", points=["p"], type="tool", domains=["llm"], stacks=[],
                                          entities=[], lang="en")])
    cap: dict = {}
    _fake_anthropic(monkeypatch, out, capture=cap)
    before = float((session.get(LlmUsage, client._local_today()) or SimpleNamespace(cost_usd=0)).cost_usd or 0)
    parsed, meta = client.structured(session, EnrichBatchOut, system="sys", user="u", model="claude-haiku-4-5")
    assert parsed is out
    assert cap["output_format"] is EnrichBatchOut
    assert cap["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert meta["tokens_in"] == 1500 and meta["tokens_out"] == 200 and meta["cost"] > 0
    u = session.get(LlmUsage, client._local_today())
    assert u.calls >= 1 and float(u.cost_usd) == pytest.approx(before + meta["cost"], abs=1e-6)


def test_structured_budget_exceeded(session, monkeypatch):
    from techradar.llm import client
    from techradar.llm.schemas import EnrichBatchOut
    _fake_anthropic(monkeypatch, None)
    u = client.today_usage(session)
    u.cost_usd = 10_000
    session.flush()
    with pytest.raises(client.BudgetExceeded):
        client.structured(session, EnrichBatchOut, system="s", user="u", model="claude-haiku-4-5")


def test_structured_refusal_and_none(session, monkeypatch):
    from techradar.llm import client
    from techradar.llm.schemas import EnrichBatchOut
    _fake_anthropic(monkeypatch, None, stop_reason="refusal")
    with pytest.raises(RuntimeError):
        client.structured(session, EnrichBatchOut, system="s", user="u", model="claude-haiku-4-5")


# ---------------- enrich ----------------
def test_enrich_count_mismatch_marks_task_failed(session, monkeypatch):
    from techradar.llm import client
    from techradar.llm.schemas import EnrichBatchOut, EnrichOut
    from techradar.models import AgentTask
    from techradar.pipeline import enrich
    it = _item("enrich-mismatch-r3", status="scored")
    it.score = 9.9
    session.add(it)
    session.flush()
    monkeypatch.setattr(session, "commit", lambda: session.flush())  # keep rollback-able
    from techradar.settings import get_settings
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "test-key")
    monkeypatch.setattr(get_settings(), "llm_provider", "anthropic")
    _fake_anthropic(monkeypatch, EnrichBatchOut(items=[]))
    st = enrich.run_enrich(session, limit=1, batch=1)
    assert st.batches == 0 and st.errors and "count mismatch" in st.errors[0]
    t = session.scalar(__import__("sqlalchemy").select(AgentTask).where(AgentTask.type == "enrich_batch")
                       .order_by(AgentTask.id.desc()))
    assert t.status == "failed" and "count mismatch" in (t.error or "")
    assert it.enrich_model is None

    # happy path with an invalid enum → falls back to 'other' and status queued→enriched
    good = EnrichBatchOut(items=[EnrichOut(summary_one="一句话", points=["a", "b"], type="nonsense",
                                           domains=["llm", "bogus"], stacks=["python"], entities=["NotACandidate"], lang="zh")])
    it.status = "queued"
    _fake_anthropic(monkeypatch, good)
    st = enrich.run_enrich(session, limit=1, batch=1)
    assert st.batches == 1 and it.enrich_model == client.model_enrich()
    assert it.tags["type"] == "other" and "bogus" not in it.tags["domains"]
    assert not (it.entities_matched or [])
    assert it.status == "enriched"


# ---------------- score edge cases ----------------
def test_negative_base_multiplier_direction_and_mute():
    hm = HeatModel({})
    old = _item("neg", sources=(("rss:foo", {}),), hours=24 * 30)   # decay dominates → base < 0
    neutral = PrefModel({}, {}, NOW)
    score_item(old, hm, neutral, NOW)
    assert old.score_breakdown["base"] < 0
    base = old.score_breakdown["base"]
    bad = PrefModel({("source", "rss:foo"): Preference(kind="source", key="rss:foo", alpha=1, beta=9)}, {}, NOW)
    score_item(old, hm, bad, NOW)
    assert old.score < base  # disliked → pushed further down, not toward zero
    muted = PrefModel({("source", "rss:foo"): Preference(kind="source", key="rss:foo", alpha=1, beta=1,
                                                       muted_until=NOW + timedelta(days=1))}, {}, NOW)
    fresh = _item("m", sources=(("rss:foo", {}),))
    score_item(fresh, hm, muted, NOW)
    assert fresh.score == 0.0 and fresh.score_breakdown["pref_mult"] == 0.0


def test_pref_multiplier_clip_bounds():
    prefs = {("source", "hackernews"): Preference(kind="source", key="hackernews", alpha=100, beta=1),
             ("author", "hackernews:bob"): Preference(kind="author", key="hackernews:bob", alpha=100, beta=1)}
    pm = PrefModel(prefs, {"stacks": {"rust": 1.5}}, NOW)
    it = _item("clip", author="bob")
    it.tags = {"stacks": ["rust"], "domains": []}
    m, _ = pm.item_multiplier(it)
    assert m == 2.0
    lo = PrefModel({("source", "hackernews"): Preference(kind="source", key="hackernews", alpha=1, beta=100),
                    ("author", "hackernews:bob"): Preference(kind="author", key="hackernews:bob", alpha=1, beta=100)},
                   {"stacks": {"rust": 0.5}}, NOW)
    m, _ = lo.item_multiplier(it)
    assert m == 0.25


def test_heat_neutral_below_20_samples():
    hm = HeatModel({"hackernews": [1.0] * 19})
    assert hm.item_heat(_item("h", sources=(("hackernews", {"points": 999}),)))[0] == 0.5


# ---------------- feedback / hide_similar ----------------
def test_hide_similar_same_author_hides_unrelated_title(session):
    from techradar.services.feedback import record_feedback
    a = _item("r3 ignore me alpha", author="r3author", status="scored")
    b = _item("completely different topic zzz", author="r3author", status="scored")
    c = _item("r3 ignore me alpha beta", author="other", status="scored")     # jaccard high
    d = _item("r3 ignore me alpha", author="other", status="digested", key="k:r3-dup")  # digested: untouched
    for x in (a, b, c, d):
        session.add(x)
    session.flush()
    r = record_feedback(session, a.id, "ignore", channel="cli")
    assert r["hidden_similar"] == 1   # only c (title similarity); b same HN author but unrelated → kept
    assert b.status == "scored" and c.status == "expired" and d.status == "digested"


def test_list_inbox_unsave(session):
    from techradar.services.feedback import list_inbox, record_feedback
    it = _item("r3 inbox item", status="scored")
    session.add(it)
    session.flush()
    record_feedback(session, it.id, "save")
    session.flush()
    assert any(r["id"] == it.id for r in list_inbox(session))
    fb = record_feedback(session, it.id, "unsave")
    # ensure ts ordering: server_default now() inside one txn is identical → unsave.ts > save.ts is False
    session.flush()
    from techradar.models import Feedback
    f = session.get(Feedback, fb["feedback_id"])
    f.ts = f.ts + timedelta(seconds=1)
    session.flush()
    assert not any(r["id"] == it.id for r in list_inbox(session))


# ---------------- digest ----------------
def test_digest_hard_limits_and_persist(session, monkeypatch):
    from techradar.digest import daily
    items = []
    for i in range(30):
        it = _item(f"r3 digest {i}", status="scored", sources=(("hackernews", {"points": 10}),))
        it.score = 10 - i * 0.1
        it.score_breakdown = {"sub_hit": 1 if i % 3 else 0}
        it.reasons = ["命中订阅: x"]
        session.add(it)
        items.append(it)
    session.flush()
    d = daily.select_digest(session)
    assert len(d.top) <= 8 and len(d.folded) <= 10
    assert len(d.explore) == 1 and all(x.reasons for x in d.top)
    md = daily.render_markdown(d)
    assert "💰 今日 LLM 花费" in md and "📦 更多" in md
    dg = daily.persist_digest(session, d, md, sent=False)
    assert all(x.status == "digested" for x in d.top + d.folded)
    # re-run same day: today's own digest is not excluded → reproduces the same set (stable rebuild)
    d2 = daily.select_digest(session)
    assert {x.id for x in d2.top} == {x.id for x in d.top}


# ---------------- telegram ----------------
def test_send_digest_split_and_keyboard(monkeypatch):
    from techradar.bot import telegram as tg
    from techradar import settings as st
    sent = []

    class _Bot:
        def __init__(self, token):
            pass

        async def send_message(self, **kw):
            sent.append(kw)

    import telegram
    monkeypatch.setattr(telegram, "Bot", _Bot)
    monkeypatch.setattr(st, "get_settings", lambda: SimpleNamespace(telegram_bot_token="t", telegram_chat_id="1"))
    monkeypatch.setattr(tg, "get_settings", lambda: SimpleNamespace(telegram_bot_token="t", telegram_chat_id="1"))
    md = "\n".join(f"line {i} " + "x" * 100 for i in range(50))   # 5400 chars → 2 parts
    d = SimpleNamespace(top=[SimpleNamespace(id=i) for i in range(8)])
    assert tg.send_digest(md, d)
    assert len(sent) == 2 and all(len(m["text"]) <= 4096 for m in sent)
    assert sent[0]["reply_markup"] is None and sent[-1]["reply_markup"] is not None
    kb = sent[-1]["reply_markup"].inline_keyboard
    assert len(kb) == 1 and [b.callback_data for b in kb[0]] == ["act:save", "act:dig", "act:ignore"]
    assert all(tg.CB_RE.match(b.callback_data) for row in kb for b in row)


def test_markdown_title_breaks_legacy_markdown():
    """Titles with unbalanced '_' / '*' / ']' break ParseMode.MARKDOWN → bot falls back to plain text with raw
    '[title](url)' markup visible. Documented risk; asserts the render does not escape."""
    from techradar.digest.daily import render_markdown, DigestData
    it = _item("foo_bar [baz] *qux*", status="scored")
    it.id = 1
    d = DigestData(day=date.today(), top=[it])
    md = render_markdown(d)
    assert "foo_bar [baz] *qux*" in md   # no escaping performed


def test_mcp_tools_registered():
    from techradar.mcp_server import mcp
    names = {t.name for t in asyncio.run(mcp.list_tools())}
    assert {"search_items", "get_item", "feedback", "mute", "add_url", "usage", "trigger_research", "get_task",
            "list_sources_health", "list_inbox", "get_digest"} <= names
