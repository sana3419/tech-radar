from __future__ import annotations

import logging

import typer
from rich import print as rprint
from rich.table import Table

app = typer.Typer(help="TechRadar CLI", no_args_is_help=True)
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")


@app.command()
def db_check():
    """Ping database."""
    from .db import ping
    rprint("[green]db ok[/green]" if ping() else "[red]db fail[/red]")


@app.command()
def db_init():
    """Create pgvector extension and all tables (dev shortcut; prefer alembic in prod)."""
    from sqlalchemy import text
    from .db import get_engine
    from .models import Base
    eng = get_engine()
    with eng.begin() as c:
        c.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        c.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.create_all(eng)
    with eng.begin() as c:
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_items_title_trgm ON items USING gin (title gin_trgm_ops)"))
        c.execute(text("CREATE INDEX IF NOT EXISTS ix_items_summary_trgm ON items USING gin (summary_one gin_trgm_ops)"))
    rprint("[green]tables created[/green]")


@app.command()
def fetch(source: str = typer.Argument("all", help="hackernews|github|rss|all"), dry: bool = False):
    """Run fetchers and ingest into DB."""
    from .db import session_scope
    from .fetchers.base import all_fetchers, get_fetcher
    from .pipeline.ingest import ingest_result
    from .settings import get_subscriptions

    src_cfg = get_subscriptions().sources
    names = all_fetchers() if source == "all" else [source]
    for name in names:
        cfg = src_cfg.get(name, {})
        if cfg.get("enabled") is False:
            rprint(f"[yellow]{name}: disabled[/yellow]")
            continue
        f = get_fetcher(name, cfg)
        if dry:
            res = f.run()
        else:
            from .pipeline.ingest import budget_check
            with session_scope() as s0:
                res = f.run(budget_check=lambda src, b: budget_check(s0, src, b))
        if dry:
            rprint(f"{name}: {len(res.items)} items, calls={res.calls}, error={res.error}")
            for it in res.items[:5]:
                rprint(f"  - {it.title[:80]}  {it.metrics}")
            continue
        with session_scope() as s:
            st = ingest_result(s, res)
        color = "green" if res.ok else "red"
        rprint(f"[{color}]{name}[/{color}]: received={st.received} new={st.new_items} merged={st.merged_items} "
               f"new_sources={st.new_sources} snapshots={st.snapshots} calls={res.calls} err={res.error}")
        for pe in res.partial_errors:
            rprint(f"  [yellow]partial: {pe}[/yellow]")


@app.command()
def health():
    """Show source health."""
    from .db import session_scope
    from .services.health import list_sources_health
    t = Table("source", "last_success", "items", "fails", "month_calls", "error")
    with session_scope() as s:
        for r in list_sources_health(s):
            t.add_row(r["source"], (r["last_success_at"] or "-")[:19], str(r["last_items"]),
                      str(r["consecutive_failures"]), str(r["month_calls"]), (r["last_error"] or "")[:40])
    rprint(t)


@app.command()
def stats():
    """Quick counts."""
    from sqlalchemy import func, select
    from .db import session_scope
    from .models import Item, ItemSource, Snapshot
    with session_scope() as s:
        n_items = s.scalar(select(func.count()).select_from(Item))
        n_src = s.scalar(select(func.count()).select_from(ItemSource))
        n_snap = s.scalar(select(func.count()).select_from(Snapshot))
        by_status = s.execute(select(Item.status, func.count()).group_by(Item.status)).all()
        multi = s.scalar(
            select(func.count()).select_from(
                select(ItemSource.item_id).group_by(ItemSource.item_id).having(func.count() > 1).subquery()
            )
        )
    rprint(f"items={n_items} item_sources={n_src} snapshots={n_snap} multi_source_items={multi}")
    rprint(dict(by_status))


@app.command(name="filter")
def filter_cmd():
    """Rule filter: new → queued/filtered."""
    from .db import session_scope
    from .pipeline.filter import run_filter
    with session_scope() as s:
        rprint(run_filter(s))


@app.command()
def fold():
    """Group near-duplicate cross-source items into events."""
    from .db import session_scope
    from .pipeline.events import fold_events
    with session_scope() as s:
        rprint({"grouped": fold_events(s)})


@app.command()
def score(hours: int = 72):
    """Rule ranker over recent queued/scored items."""
    from .db import session_scope
    from .pipeline.score import run_score
    with session_scope() as s:
        rprint(run_score(s, hours=hours))


@app.command()
def top(n: int = 20):
    """Show top ranked items with reasons."""
    from .db import session_scope
    from .pipeline.score import top_items
    t = Table("score", "kind", "title", "sources", "reasons", show_lines=False)
    with session_scope() as s:
        for it in top_items(s, n):
            srcs = ",".join(sorted({x.source.split(":")[0] for x in it.sources}))
            t.add_row(f"{it.score:.2f}", it.kind, (it.title or "")[:60], srcs, "; ".join(it.reasons or [])[:60])
    rprint(t)


@app.command()
def run():
    """Run the scheduler (fetch/snapshot/enrich/digest/expire/research)."""
    from .scheduler import run as _run
    _run()


@app.command()
def mcp():
    """Run MCP server over stdio."""
    from .mcp_server import main
    main()


@app.command()
def web(host: str = "127.0.0.1", port: int = 8765):
    """Run the web UI (today's unread stream, inbox, search)."""
    from .web.app import run as _run
    _run(host, port)


@app.command()
def bot():
    """Run Telegram bot (polling): buttons, search, /add, /mute, /inbox, /dig."""
    from .bot.telegram import run_polling
    run_polling()


@app.command()
def digest(send: bool = False, persist: bool = False, force: bool = False):
    """Build today's digest. --send pushes to Telegram; --persist records digest_items (automatic when sending).
    Same-day rerun reuses the persisted set unless --force."""
    from .db import session_scope
    from .digest.daily import ensure_enriched, load_persisted, persist_digest, render_markdown, select_digest
    with session_scope() as s:
        d = (None if force else load_persisted(s)) or select_digest(s)
        if send or persist:
            ensure_enriched(s, d)
        md = render_markdown(d)
        print(md)
        if send:
            from .bot.telegram import send_digest
            ok = send_digest(render_markdown(d, html=True), d)
            rprint("[green]sent[/green]" if ok else "[red]send failed[/red]")
            persist_digest(s, d, md, sent=ok)
        elif persist:
            persist_digest(s, d, md, sent=False)


@app.command()
def weekly(send: bool = False):
    """Build weekly review; --send pushes to Telegram."""
    from .db import session_scope
    from .digest.weekly import build_weekly, persist_weekly, render_weekly
    with session_scope() as s:
        w = build_weekly(s)
        md = render_weekly(w)
        print(md)
        ok = False
        if send:
            from .bot.telegram import send_text_html
            ok = send_text_html(render_weekly(w, html=True))
            rprint("[green]sent[/green]" if ok else "[red]failed[/red]")
        persist_weekly(s, w, md, sent=ok)


@app.command()
def enrich(limit: int = 200):
    """LLM structured summaries/tags for pending items (respects daily budget)."""
    from .db import session_scope
    from .pipeline.enrich import run_enrich
    with session_scope() as s:
        rprint(run_enrich(s, limit=limit))


@app.command()
def usage():
    from .db import session_scope
    from .services.usage import usage as _u
    with session_scope() as s:
        rprint(_u(s))


@app.command()
def feedback(item_id: int, action: str, note: str = None):
    """Record feedback: save|ignore|read|click|expand|dig|unsave."""
    from .db import session_scope
    from .services.feedback import record_feedback
    with session_scope() as s:
        rprint(record_feedback(s, item_id, action, channel="cli", note=note))


@app.command()
def search(query: str, limit: int = 10, saved: bool = False):
    """Full-text search over items."""
    from .db import session_scope
    from .services.items import search_items
    with session_scope() as s:
        for r in search_items(s, query, only_saved=saved, limit=limit):
            rprint(f"#{r['id']} [{r['kind']}] {r['title'][:70]}  {r['url']}")


@app.command()
def ask(question: str):
    """Grounded Q&A over local memory."""
    from .agents.chat import ask as _ask
    from .db import session_scope
    with session_scope() as s:
        r = _ask(s, question)
    rprint(r["answer"])
    for c in r["citations"]:
        rprint(f"  [{c['n']}] {c['title'][:60]}  {c['url']}")
    rprint(f"(cost ${r['cost']:.4f})")


@app.command()
def sync_config():
    """Sync subscriptions.yaml entities into DB + refresh timeline."""
    from .db import session_scope
    from .services.entities import sync_entities, update_timeline
    with session_scope() as s:
        rprint({"entities": sync_entities(s), "timeline_added": update_timeline(s, hours=24 * 14)})


@app.command()
def brief(force: bool = False, limit: int = 20):
    """Refresh agent-written 'current state' cards for tracked entities."""
    from .agents.brief import refresh_all
    from .db import session_scope
    with session_scope() as s:
        rprint(refresh_all(s, force=force, limit=limit))


@app.command()
def obsidian():
    """Render Obsidian projection (entities/digests/index)."""
    from .agents.brief import refresh_all
    from .agents.moc import build_all
    from .db import session_scope
    from .render.obsidian import render_all, vault_dir
    from .services.entities import sync_entities, update_timeline
    with session_scope() as s:
        sync_entities(s); update_timeline(s)
        rprint(refresh_all(s))
        mocs, complete = build_all(s)
        rprint(render_all(s, mocs, mocs_complete=complete), str(vault_dir()))


@app.command()
def research(item_id: int = None, question: str = None, pending: bool = False):
    """Run research: --item-id N (sync, prints report) or --pending (process queue)."""
    from .db import session_scope
    from .agents.research import run_pending, run_task
    from .models import AgentTask
    with session_scope() as s:
        if pending:
            rprint(f"processed {run_pending(s, max_tasks=5)}")
            return
        t = AgentTask(type="research", payload={"item_id": item_id, "question": question}, status="running")
        s.add(t); s.flush()
        res = run_task(s, t)
        t.status = "done"; t.result = {"report": res["report"], "path": res["path"]}
        print(res["report"]); rprint(f"(cost ${res['meta']['cost']:.4f}, saved {res['path']})")


@app.command()
def inbox():
    from .db import session_scope
    from .services.feedback import list_inbox
    with session_scope() as s:
        for r in list_inbox(s):
            rprint(f"#{r['id']} {r['title'][:70]}  ({r['saved_at'][:10]}) {r['note'] or ''}")


@app.command()
def snapshot(limit: int = 300):
    """Tiered metric revisit for recent items."""
    from .db import session_scope
    from .pipeline.snapshot import refresh_snapshots
    with session_scope() as s:
        rprint(refresh_snapshots(s, limit=limit))


@app.command()
def expire(hours: int = typer.Option(None)):
    """Expire unread scored/digested items older than N hours (default from settings)."""
    from .db import session_scope
    from .pipeline.lifecycle import expire_unread
    from .settings import get_settings
    with session_scope() as s:
        n = expire_unread(s, hours or get_settings().unread_expire_hours)
    rprint(f"expired {n}")


@app.command()
def gc():
    """Delete orphan items (no item_sources) — e.g. after canonical rule changes."""
    from sqlalchemy import delete, select, exists
    from .db import session_scope
    from .models import Feedback, Item, ItemSource
    with session_scope() as s:
        sub = select(ItemSource.id).where(ItemSource.item_id == Item.id)
        fb = select(Feedback.id).where(Feedback.item_id == Item.id)
        n = s.execute(delete(Item).where(~exists(sub), ~exists(fb))).rowcount
    rprint(f"deleted {n} orphan items")


if __name__ == "__main__":
    app()
