"""W2 batch-1 acceptance: Telegram interaction (mocked), digest diversity + numbering, RSS release feeds, search."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from techradar.models import Item, ItemSource
from tests.test_ingest import session  # noqa: F401

NOW = datetime.now(timezone.utc)


# ---------------- telegram (mock) ----------------
class _Chat:
    async def send_action(self, *a, **k):
        pass


class _Bot:
    def __init__(self):
        self.sent = []

    async def send_message(self, chat_id, text, **kw):
        self.sent.append(text)


class _Msg:
    def __init__(self, text=""):
        self.text = text
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)

    @property
    def chat(self):
        return _Chat()


def _update(text="", chat_id="1", cb=None):
    msg = _Msg(text)
    q = None
    if cb is not None:
        q = SimpleNamespace(data=cb, message=msg, answered=[])

        async def _ans(t=None):
            q.answered.append(t)
        q.answer = _ans
    return SimpleNamespace(message=msg, effective_chat=SimpleNamespace(id=chat_id), callback_query=q)


@pytest.fixture
def tg(monkeypatch):
    from techradar.bot import telegram as tg
    monkeypatch.setattr(tg, "get_settings", lambda: SimpleNamespace(telegram_bot_token="t", telegram_chat_id="1"))
    return tg


def _ctx(args=None):
    return SimpleNamespace(chat_data={}, args=args or [], bot=_Bot())


def _patch_apply(monkeypatch):
    calls = []
    from techradar.digest import daily
    from techradar.services import feedback, research
    monkeypatch.setattr(daily, "resolve_positions", lambda s, nums, day=None: {n: (100 + n if n <= 5 else None) for n in nums})
    monkeypatch.setattr(feedback, "record_feedback", lambda s, iid, action, channel=None, **kw: calls.append((iid, action)) or {"hidden_similar": 0})
    monkeypatch.setattr(research, "enqueue_research", lambda s, item_id=None, question=None: calls.append(("dig-enq", item_id)) or {"task_id": 7})
    return calls


def test_callback_then_numbers(tg, monkeypatch):
    calls = _patch_apply(monkeypatch)
    ctx = _ctx()
    up = _update(cb="act:save")
    asyncio.run(tg.on_callback(up, ctx))
    assert ctx.chat_data["pending_action"] == "save" and up.callback_query.answered
    up2 = _update("1 3 5 9")
    asyncio.run(tg.on_text(up2, ctx))
    assert [c for c in calls if c[1] == "save"] == [(101, "save"), (103, "save"), (105, "save")]
    assert "已收藏 1, 3, 5" in up2.message.replies[-1] and "没有编号 9" in up2.message.replies[-1]
    assert "pending_action" not in ctx.chat_data


def test_inline_action_prefix_and_dig(tg, monkeypatch):
    calls = _patch_apply(monkeypatch)
    up = _update("收藏 3")
    asyncio.run(tg.on_text(up, _ctx()))
    assert (103, "save") in calls
    up = _update("深挖3")
    asyncio.run(tg.on_text(up, _ctx()))
    assert ("dig-enq", 103) in calls and (103, "dig") in calls
    assert "完成后会推送报告" in up.message.replies[-1]


def test_numbers_without_action_prompts(tg, monkeypatch):
    calls = _patch_apply(monkeypatch)
    up = _update("3")
    asyncio.run(tg.on_text(up, _ctx()))
    assert not calls and "先点日报下方的按钮" in up.message.replies[-1]


def test_plain_text_goes_to_answer(tg, monkeypatch):
    seen = []

    async def _fake_answer(update, q):
        seen.append(q)
    monkeypatch.setattr(tg, "_answer", _fake_answer)
    asyncio.run(tg.on_text(_update("vllm 最近有什么更新？"), _ctx()))
    assert seen == ["vllm 最近有什么更新？"]


def test_unauthorized_ignored(tg, monkeypatch):
    calls = _patch_apply(monkeypatch)
    up = _update("收藏 3", chat_id="999")
    asyncio.run(tg.on_text(up, _ctx()))
    assert not calls and not up.message.replies
    up = _update(cb="act:save", chat_id="999")
    ctx = _ctx()
    asyncio.run(tg.on_callback(up, ctx))
    assert up.callback_query.answered == ["unauthorized"] and "pending_action" not in ctx.chat_data


def test_answer_renders_citations_and_handles_error(tg, monkeypatch):
    from techradar.agents import chat
    monkeypatch.setattr(chat, "ask", lambda s, q, **kw: {"answer": "A <b> [1]", "citations": [{"n": 1, "id": 1, "title": "T&", "url": "https://x/?a=1&b=2"}], "cost": 0, "web_count": 0})
    monkeypatch.setattr(tg, "session_scope", lambda: __import__("contextlib").nullcontext(None))
    import techradar.services.chatlog as _cl
    monkeypatch.setattr(_cl, "recent_turns", lambda *a, **k: [])
    monkeypatch.setattr(_cl, "record_turn", lambda *a, **k: None)
    up = _update("q")
    asyncio.run(tg._answer(up, "q"))
    r = up.message.replies[-1]
    assert "A &lt;b&gt; [1]" in r and 'href="https://x/?a=1&amp;b=2"' in r and "T&amp;" in r

    def _boom(s, q, **kw):
        raise RuntimeError("x")
    monkeypatch.setattr(chat, "ask", _boom)
    up = _update("q")
    asyncio.run(tg._answer(up, "q"))
    assert "回答失败" in up.message.replies[-1]


def test_commands_menu_is_chinese(tg):
    assert {c for c, _ in tg.COMMANDS} >= {"start", "search", "dig", "today", "health", "inbox", "add", "mute"}
    assert all(any("一" <= ch <= "鿿" for ch in d) for _, d in tg.COMMANDS)


def test_num_re_edge_cases(tg):
    assert tg.NUM_RE.match("1，3、5")
    assert tg.NUM_RE.match("ignore 2")
    assert not tg.NUM_RE.match("3 篇论文")
    assert not tg.NUM_RE.match("收藏 abc")


# ---------------- digest ----------------
def _item(title, key, kind="article", src="hackernews", score=5.0, sub=1, status="scored"):
    it = Item(title=title, url=f"https://x/{key}", canonical_key=key, kind=kind, status=status, score=score,
              score_breakdown={"sub_hit": sub}, first_seen_at=NOW, last_seen_at=NOW, published_at=NOW)
    it.sources = [ItemSource(source=src, external_id=key, metrics_raw={}, seen_at=NOW, raw={})]
    return it


def test_digest_diversity_and_numbering(session):
    from techradar.digest import daily
    items = []
    for i in range(6):   # 6 releases from 2 repos
        repo = "vllm-project/vllm" if i % 2 else "ggml-org/llama.cpp"
        items.append(_item(f"rel {i}", f"gh:{repo}#tag/v{i}", kind="release", src="rss:gh_x", score=20 - i))
    for i in range(6):
        items.append(_item(f"paper {i}", f"arxiv:2608.{i:05d}", kind="paper", src="rss:arxiv_cs_ai", score=15 - i))
    for i in range(10):
        items.append(_item(f"hn {i}", f"url:hn{i}", src="hackernews", score=12 - i * 0.1))
    for i in range(6):
        items.append(_item(f"gh {i}", f"gh:o/r{i}", kind="repo", src="github", score=9 - i * 0.1))
    session.add_all(items)
    session.flush()
    d = daily.select_digest(session)
    kinds = [x.kind for x in d.top]
    assert kinds.count("paper") <= 3 and kinds.count("release") <= 2
    fams = [daily._family(x) for x in d.top]
    assert max(fams.count(f) for f in set(fams)) <= 4
    repos = [daily.select_digest.__globals__ and x.canonical_key.split("#")[0] for x in d.top + d.folded if x.kind == "release"]
    assert len(repos) == len(set(repos)), "one release per repo across top+folded"
    md = daily.render_markdown(d)
    nums = [int(l.split(".")[0]) for l in md.splitlines() if l[:1].isdigit()]
    assert nums == list(range(1, len(d.top) + len(d.folded) + 1))
    html = daily.render_markdown(d, html=True)
    assert "<a href=" in html and "[HN]" in html
    dg = daily.persist_digest(session, d, md, sent=False)
    m = daily.resolve_positions(session, [1, len(d.top) + 1, 999], day=d.day)
    assert m[1] == d.top[0].id and m[len(d.top) + 1] == d.folded[0].id and m[999] is None


def test_local_today_uses_config_tz(monkeypatch):
    from techradar.digest import daily
    monkeypatch.setattr(daily, "get_settings", lambda: SimpleNamespace(timezone="Pacific/Kiritimati"))
    a = daily.local_today()
    monkeypatch.setattr(daily, "get_settings", lambda: SimpleNamespace(timezone="Etc/GMT+12"))
    b = daily.local_today()
    assert a - b in (timedelta(days=1), timedelta(days=2))


# ---------------- rss release feeds ----------------
def test_entity_release_feeds_generated():
    from techradar.fetchers.rss import RSSFetcher
    from techradar.settings import get_subscriptions
    feeds = RSSFetcher(get_subscriptions().sources["rss"])._feeds()
    rel = [f for f in feeds if f.get("kind") == "release"]
    assert {f["id"] for f in rel} >= {"gh_release_vllm_project_vllm", "gh_release_ggml_org_llama_cpp"}
    assert all(f["url"].endswith("/releases.atom") and f["entity"] for f in rel)
    assert len({f["id"] for f in feeds}) == len(feeds)


def test_release_canonical_key():
    from techradar.pipeline.canonical import canonical_key
    assert canonical_key("https://github.com/ggml-org/llama.cpp/releases/tag/b10488") == ("gh:ggml-org/llama.cpp#tag/b10488", "release")


def test_release_title_prefixed(monkeypatch):
    from techradar.fetchers.rss import RSSFetcher
    atom = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><entry><id>tag:github.com,2008:Repository/1/b1</id>
    <title>v1.2.0</title><link href="https://github.com/ggml-org/llama.cpp/releases/tag/v1.2.0"/><updated>2026-08-18T00:00:00Z</updated></entry></feed>"""
    f = RSSFetcher({"feeds": [], "entity_releases": True})
    monkeypatch.setattr(f, "_feeds", lambda: [{"id": "gh_release_x", "url": "u", "label": "llama.cpp", "kind": "release", "entity": "llama.cpp", "limit": 5}])
    monkeypatch.setattr(f, "get", lambda url: SimpleNamespace(content=atom))
    items = list(f.fetch())
    assert items[0].title == "llama.cpp v1.2.0" and items[0].kind == "release" and items[0].source == "rss:gh_release_x"


# ---------------- search / chat ----------------
def test_search_cjk_and_ascii(session):
    from techradar.services.items import search_items
    a = _item("Linux 7.3 improves vRAM", "url:t1"); a.summary_one = "优化显存不足时的性能"
    b = _item("Learning KV-Cache Management", "url:t2"); b.summary_one = "优化KV缓存管理"
    session.add_all([a, b]); session.flush()
    ids = lambda q, **k: [r["id"] for r in search_items(session, q, **k)]
    assert a.id in ids("显存") and b.id not in ids("显存")
    assert b.id in ids("kv cache") and b.id in ids("KV缓存")
    assert ids("显存", only_saved=True) == []


def test_chat_keywords_and_time_hint():
    from techradar.agents.chat import _keywords, _time_hint
    assert "什么" not in _keywords("最近有什么关于 agent 的论文？") and "agent" in _keywords("最近有什么关于 agent 的论文？")
    assert _time_hint("这周有什么") is not None and _time_hint("vllm") is None


def test_chat_drops_out_of_range_citations(session, monkeypatch):
    from techradar.agents import chat
    monkeypatch.setattr(chat, "search_items", lambda *a, **k: [{"id": 1, "title": "T", "url": "u", "sources": [], "first_seen_at": "2026-08-18", "summary_one": None}])
    monkeypatch.setattr(chat, "structured", lambda *a, **k: (chat.ChatOut(answer="x [1] y [7]", used=[1, 7]), {"cost": 0.0}))
    r = chat.ask(session, "q", web=False)      # offline: web hits would make [7] a valid citation
    assert r["answer"] == "x [1] y" and [c["n"] for c in r["citations"]] == [1]
