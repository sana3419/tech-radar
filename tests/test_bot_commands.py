"""Smoke-test every Telegram handler with fake updates (no network)."""
import asyncio
from types import SimpleNamespace

import pytest

from tests.test_bot_w2 import _ctx, _update, tg  # noqa: F401
from tests.test_ingest import session, UNIQ  # noqa: F401


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def db(monkeypatch, session, tg):
    """Route bot's session_scope to the rollback-able test session."""
    from contextlib import contextmanager

    @contextmanager
    def _scope():
        yield session
    monkeypatch.setattr(tg, "session_scope", _scope)
    return session


def _mk_item(session, title="vLLM 0.9 released", summary="vLLM 0.9 发布，MoE 推理提速 2 倍"):
    from datetime import datetime, timezone
    from techradar.fetchers.base import RawItem
    from techradar.pipeline.ingest import ingest_raw
    it, _, _ = ingest_raw(session, RawItem(source="hackernews", external_id=f"cmd{UNIQ[:6]}{abs(hash(title)) % 1000}",
                                           title=title, url=f"https://e.com/{UNIQ}-{abs(hash(title)) % 1000}",
                                           metrics={"points": 50}), datetime.now(timezone.utc), expand_short=False)
    it.summary_one = summary
    it.status = "scored"
    it.score = 5.0
    session.flush()
    return it


def test_start_and_health_and_inbox(db, tg):
    up = _update("/start"); _run(tg.cmd_start(up, _ctx())); assert "chat_id" in up.message.replies[0]
    up = _update("/health"); _run(tg.cmd_health(up, _ctx())); assert up.message.replies
    up = _update("/inbox"); _run(tg.cmd_inbox(up, _ctx())); assert up.message.replies


def test_search_format_has_intro_link_and_source(db, tg):
    it = _mk_item(db)
    up = _update("/search vllm"); _run(tg.cmd_search(up, _ctx(["vllm"])))
    out = up.message.replies[0]
    assert "<a href=" in out and "vLLM 0.9 发布" in out and "[HN]" in out and f"#{it.id}" in out


def test_dig_by_item_id_text(db, tg):
    it = _mk_item(db, title="dig me")
    up = _update(f"深挖 #{it.id}"); _run(tg.on_text(up, _ctx()))
    assert "已深挖" in up.message.replies[0]
    from sqlalchemy import select
    from techradar.models import AgentTask
    t = db.scalar(select(AgentTask).where(AgentTask.type == "research").order_by(AgentTask.id.desc()))
    assert t and t.payload["item_id"] == it.id
    up = _update("收藏 #999999999"); _run(tg.on_text(up, _ctx()))
    assert "没有" in up.message.replies[0]


def test_mute_and_today(db, tg, monkeypatch):
    up = _update("/mute hackernews 3"); _run(tg.cmd_mute(up, _ctx(["hackernews", "3"])))
    assert "已静音" in up.message.replies[0]
    # /today: avoid LLM by faking ensure_enriched
    from techradar.digest import daily
    monkeypatch.setattr(daily, "ensure_enriched", lambda s, d: None)
    _mk_item(db, title="today item")
    up = _update("/today"); _run(tg.cmd_today(up, _ctx()))
    assert up.message.replies and "技术日报" in up.message.replies[0]


def test_numbers_without_pending_prompts(db, tg):
    up = _update("3"); _run(tg.on_text(up, _ctx()))
    assert "先点" in up.message.replies[0]
