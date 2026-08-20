"""Cron jobs must fire on the configured timezone, not the host's.

Regression: CronTrigger instances capture the host tz at construction, so the digest fired at
08:00 America/Los_Angeles instead of 08:00 Asia/Shanghai.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from techradar.scheduler import build_scheduler
from techradar.settings import get_settings

CRON_JOBS = {"fetch:hackernews", "fetch:github", "fetch:rss", "snapshot", "enrich",
             "digest", "expire", "obsidian", "weekly"}


def test_all_cron_triggers_use_configured_timezone():
    tz = ZoneInfo(get_settings().timezone)
    jobs = {j.id: j for j in build_scheduler().get_jobs()}
    assert CRON_JOBS <= set(jobs), f"missing jobs: {CRON_JOBS - set(jobs)}"
    for jid in CRON_JOBS:
        assert str(jobs[jid].trigger.timezone) == str(tz), f"{jid} uses {jobs[jid].trigger.timezone}"


def test_digest_fires_at_configured_local_hour():
    st = get_settings()
    tz = ZoneInfo(st.timezone)
    job = {j.id: j for j in build_scheduler().get_jobs()}["digest"]
    nxt = job.trigger.get_next_fire_time(None, datetime(2026, 8, 20, 13, 0, tzinfo=tz))
    assert nxt.astimezone(tz).hour == st.digest_hour and nxt.astimezone(tz).minute == 0


def test_weekly_fires_sunday_evening_local():
    tz = ZoneInfo(get_settings().timezone)
    job = {j.id: j for j in build_scheduler().get_jobs()}["weekly"]
    nxt = job.trigger.get_next_fire_time(None, datetime(2026, 8, 20, 13, 0, tzinfo=tz)).astimezone(tz)
    assert nxt.weekday() == 6 and nxt.hour == 20      # Sunday 20:00 local


def test_connections_disable_prepared_statements():
    """Regression: a migration that changed a column type crashed the running scheduler with
    'cached plan must not change result type' until it was restarted."""
    from techradar.db import get_engine
    with get_engine().connect() as c:
        raw = c.connection.dbapi_connection
        assert getattr(raw, "prepare_threshold", "missing") is None
