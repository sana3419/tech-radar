"""APScheduler-driven jobs (docs/02 §6). Run: `techradar run`."""
from __future__ import annotations

import logging
from datetime import date

from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger

from .db import session_scope
from .settings import get_settings, get_subscriptions

log = logging.getLogger(__name__)


def job_fetch(source: str):
    from .fetchers.base import get_fetcher
    from .pipeline.ingest import budget_check, ingest_result
    cfg = get_subscriptions().sources.get(source, {})
    if cfg.get("enabled") is False:
        return
    f = get_fetcher(source, cfg)
    with session_scope() as s:
        res = f.run(budget_check=lambda src, b: budget_check(s, src, b))
        st = ingest_result(s, res)
    log.info("fetch %s: new=%s merged=%s err=%s", source, st.new_items, st.merged_items, res.error)
    job_filter_score()


def job_filter_score():
    from .pipeline.events import fold_events
    from .pipeline.filter import run_filter
    from .pipeline.score import run_score
    with session_scope() as s:
        f = run_filter(s)
        ev = fold_events(s)
        sc = run_score(s)
    log.info("filter queued=%s filtered=%s; folded=%s; scored=%s", f.queued, f.filtered, ev, sc.scored)


def job_snapshot():
    from .pipeline.snapshot import refresh_snapshots
    with session_scope() as s:
        log.info("snapshot %s", refresh_snapshots(s))


def job_enrich():
    from .pipeline.enrich import run_enrich
    from .llm.client import is_configured
    if not is_configured():
        log.warning("enrich skipped: LLM not configured")
        return
    with session_scope() as s:
        st = run_enrich(s)
    log.info("enrich batches=%s items=%s cost=%.4f stop=%s errs=%s", st.batches, st.items, st.cost, st.stopped_reason, st.errors[:2])
    job_filter_score()   # re-score with tags


def job_digest():
    from sqlalchemy import select
    from .digest.daily import ensure_enriched, persist_digest, render_markdown, select_digest
    from .models import Digest
    with session_scope() as s:
        from .digest.daily import local_today
        existing = s.scalar(select(Digest).where(Digest.day == local_today(), Digest.kind == "daily", Digest.sent_at.isnot(None)))
        if existing:
            log.info("digest already sent today")
            return
        d = select_digest(s)
        ensure_enriched(s, d)
        md = render_markdown(d)
        ok = False
        if get_settings().telegram_bot_token:
            from .bot.telegram import send_digest
            ok = send_digest(render_markdown(d, html=True), d)
        persist_digest(s, d, md, sent=ok)
    log.info("digest sent=%s top=%s folded=%s", ok, len(d.top), len(d.folded))


def job_expire():
    from .pipeline.lifecycle import expire_unread
    with session_scope() as s:
        log.info("expired %s", expire_unread(s, get_settings().unread_expire_hours))


def job_weekly():
    from .digest.weekly import build_weekly, persist_weekly, render_weekly
    with session_scope() as s:
        w = build_weekly(s)
        md = render_weekly(w)
        ok = False
        if get_settings().telegram_bot_token:
            from .bot.telegram import send_text_html
            ok = send_text_html(render_weekly(w, html=True))
        persist_weekly(s, w, md, sent=ok)
    log.info("weekly sent=%s", ok)


def job_obsidian():
    from .agents.brief import refresh_all
    from .render.obsidian import render_all
    from .services.entities import sync_entities, update_timeline
    with session_scope() as s:
        sync_entities(s)
        update_timeline(s)
        log.info("entity briefs: %s", refresh_all(s))
        from .agents.moc import build_all
        mocs, complete = build_all(s)
        log.info("obsidian render: %s", render_all(s, mocs, mocs_complete=complete))


def job_research_worker():
    try:
        from .agents.research import run_pending
    except ImportError:
        return
    try:
        with session_scope() as s:
            run_pending(s, max_tasks=2)
    except Exception:  # noqa: BLE001 — a bad round must not kill the recurring job
        log.exception("research worker round failed; will retry next tick")


def build_scheduler() -> BlockingScheduler:
    from zoneinfo import ZoneInfo
    st = get_settings()
    tz = ZoneInfo(st.timezone)
    sched = BlockingScheduler(timezone=tz)

    def cron(**kw) -> CronTrigger:
        """CronTrigger bound to the configured timezone.

        A CronTrigger *instance* captures the host timezone at construction; the scheduler's own
        timezone only applies to string-style triggers. Without this the digest fired on host time.
        """
        return CronTrigger(timezone=tz, **kw)

    sources = [k for k, v in get_subscriptions().sources.items() if v.get("enabled", True)]
    for i, src in enumerate(sources):
        # hourly, staggered by source
        sched.add_job(job_fetch, cron(minute=(i * 12) % 60), args=[src], id=f"fetch:{src}",
                      misfire_grace_time=600, coalesce=True)
    sched.add_job(job_snapshot, cron(minute=50), id="snapshot", misfire_grace_time=600, coalesce=True)
    sched.add_job(job_enrich, cron(minute=5, hour="*/2"), id="enrich", misfire_grace_time=900, coalesce=True)
    sched.add_job(job_digest, cron(hour=st.digest_hour, minute=0), id="digest", misfire_grace_time=3600, coalesce=True)
    sched.add_job(job_expire, cron(hour=3, minute=30), id="expire", coalesce=True)
    sched.add_job(job_obsidian, cron(hour="9,21", minute=15), id="obsidian", coalesce=True)
    sched.add_job(job_weekly, cron(day_of_week="sun", hour=20, minute=0), id="weekly", coalesce=True)
    sched.add_job(job_research_worker, IntervalTrigger(minutes=2), id="research", coalesce=True, max_instances=1)
    return sched


def run():
    logging.getLogger("apscheduler").setLevel(logging.INFO)
    sched = build_scheduler()
    log.info("scheduler jobs: %s", [j.id for j in sched.get_jobs()])
    sched.start()
