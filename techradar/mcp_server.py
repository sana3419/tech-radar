"""MCP server exposing TechRadar core (docs/02 §5.4). Run: `techradar mcp` (stdio).
Thin wrapper over services/*; Web/bot import services directly."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from mcp.server import MCPServer

from .db import session_scope

mcp = MCPServer("techradar")


def _dt(s: str | None) -> datetime | None:
    return datetime.fromisoformat(s) if s else None


@mcp.tool()
def search_items(query: str, since: str | None = None, until: str | None = None,
                 only_saved: bool = False, limit: int = 20) -> list[dict[str, Any]]:
    """Search collected tech items (full-text). since/until ISO dates. only_saved restricts to inbox."""
    from .services.items import search_items as _s
    with session_scope() as s:
        return _s(s, query, since=_dt(since), until=_dt(until), only_saved=only_saved, limit=limit)


@mcp.tool()
def get_item(item_id: int) -> dict[str, Any] | None:
    """Item detail: sources, summary, points, score breakdown."""
    from .services.items import get_item as _g
    with session_scope() as s:
        return _g(s, item_id)


@mcp.tool()
def today_feed(limit: int = 30) -> list[dict[str, Any]]:
    """Today's unread stream ordered by score."""
    from .services.items import today_feed as _f
    with session_scope() as s:
        return _f(s, limit)


@mcp.tool()
def get_digest(day: str | None = None) -> dict[str, Any] | None:
    """Daily digest markdown for a day (YYYY-MM-DD, default today)."""
    from datetime import date
    from sqlalchemy import select
    from .models import Digest
    d = date.fromisoformat(day) if day else date.today()
    with session_scope() as s:
        dg = s.scalar(select(Digest).where(Digest.day == d, Digest.kind == "daily"))
        return {"day": d.isoformat(), "markdown": dg.markdown, "stats": dg.stats,
                "sent_at": dg.sent_at.isoformat() if dg.sent_at else None} if dg else None


@mcp.tool()
def list_inbox(limit: int = 50) -> list[dict[str, Any]]:
    """Saved items."""
    from .services.feedback import list_inbox as _i
    with session_scope() as s:
        return _i(s, limit)


@mcp.tool()
def feedback(item_id: int, action: str, note: str | None = None) -> dict[str, Any]:
    """Record feedback: save|ignore|read|click|expand|dig|unsave."""
    from .services.feedback import record_feedback
    with session_scope() as s:
        return record_feedback(s, item_id, action, channel="mcp", note=note)


@mcp.tool()
def mute(kind: str, key: str, days: int = 7) -> dict[str, Any]:
    """Mute a source/tag/author/entity for N days."""
    from .services.feedback import mute as _m
    with session_scope() as s:
        return _m(s, kind, key, days)


@mcp.tool()
def add_url(url: str, note: str | None = None) -> dict[str, Any]:
    """Manually feed a URL into the radar (also saves it)."""
    from .services.manual import add_url as _a
    with session_scope() as s:
        return _a(s, url, note)


@mcp.tool()
def list_sources_health() -> list[dict[str, Any]]:
    """Fetcher health: last success, failures, monthly calls."""
    from .services.health import list_sources_health as _h
    with session_scope() as s:
        return _h(s)


@mcp.tool()
def trigger_research(item_id: int | None = None, entity_id: int | None = None, question: str | None = None) -> dict[str, Any]:
    """Enqueue a research task; returns task_id."""
    from .services.research import enqueue_research
    with session_scope() as s:
        return enqueue_research(s, item_id=item_id, entity_id=entity_id, question=question)


@mcp.tool()
def get_task(task_id: int) -> dict[str, Any] | None:
    """Agent task status/result."""
    from .services.research import get_task as _t
    with session_scope() as s:
        return _t(s, task_id)


@mcp.tool()
def ask(question: str) -> dict[str, Any]:
    """Grounded Q&A over the local memory; returns answer with [n] citations and their items."""
    from .agents.chat import ask as _ask
    with session_scope() as s:
        return _ask(s, question)


@mcp.tool()
def usage(day: str | None = None) -> dict[str, Any]:
    """LLM usage/cost for a day."""
    from datetime import date
    from .services.usage import usage as _u
    with session_scope() as s:
        return _u(s, date.fromisoformat(day) if day else None)


def main():
    mcp.run()


if __name__ == "__main__":
    main()
