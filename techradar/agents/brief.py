"""Entity brief: an agent-written "current state" card for each tracked entity.

Reads the entity's recent timeline (facts already summarized by enrich) and writes a short
status/activity/trend/advice card into entities.brief. Refreshed only when the timeline grew,
so a quiet entity costs nothing.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..llm.client import BudgetExceeded, is_configured, model_enrich, structured
from ..llm.schemas import EntityBriefOut
from ..models import Entity, EntityTimeline, Item

log = logging.getLogger(__name__)
PROMPT_VERSION = "brief-v1"
TIMELINE_N = 25

SYSTEM = """你在为一位后端/AI 方向的开发者维护技术情报档案。根据给定的某个项目/技术的近期记录，写一张"当前状态卡"。
要求：
- 中文；专有名词、版本号保留原文；只依据给定记录，不要编造未提及的数字或事实。
- status：这个东西现在处于什么状态（成熟度、最新版本、能力边界），≤80 字。
- activity：最近在做什么（近期变更、讨论焦点），≤60 字。
- trend：必须以「活跃度.判定」给出的词开头（升温/平稳/降温），后跟一句依据，依据要引用本周/上周条数。不要自己改判定。
- advice：对该开发者的跟进建议，≤50 字，要具体（例如"值得在 24G 显存机器上验证"而不是"值得关注"）。
- highlights：0-3 条最值得知道的具体事实（带数字更好），每条 ≤30 字。
- 如果记录很少或信息不足，如实说明，不要硬凑。"""


def _trend_stats(session: Session, entity_id: int) -> dict:
    """7d vs previous 7d volume — the model shouldn't guess a trend from an undated list."""
    from datetime import timedelta
    now = datetime.now(timezone.utc)
    def _n(a, b):
        return session.scalar(
            select(func.count()).select_from(EntityTimeline)
            .where(EntityTimeline.entity_id == entity_id, EntityTimeline.ts >= a, EntityTimeline.ts < b)
        ) or 0
    cur = _n(now - timedelta(days=7), now)
    prev = _n(now - timedelta(days=14), now - timedelta(days=7))
    if cur == 0 and prev == 0:
        label = "平稳"
    elif cur > prev * 1.5 and cur >= 2:
        label = "升温"
    elif cur * 1.5 < prev:
        label = "降温"
    else:
        label = "平稳"
    return {"本周条数": cur, "上周条数": prev, "判定": label}


def _timeline_payload(session: Session, e: Entity, n: int = TIMELINE_N) -> tuple[str, int]:
    rows = session.execute(
        select(EntityTimeline, Item).join(Item, Item.id == EntityTimeline.item_id)
        .where(EntityTimeline.entity_id == e.id).order_by(EntityTimeline.ts.desc()).limit(n)
    ).all()
    total = session.scalar(
        select(func.count()).select_from(EntityTimeline).where(EntityTimeline.entity_id == e.id)
    ) or 0
    recs = [{
        "date": tl.ts.isoformat()[:10],
        "type": tl.event_type,
        "text": (it.summary_one or it.title)[:120],
        "points": (it.summary_points or [])[:2],
    } for tl, it in rows]
    payload = json.dumps({"实体": e.canonical_name, "类型": e.type,
                          "锚点": e.anchors or {}, "记录总数": total,
                          "活跃度": _trend_stats(session, e.id), "近期记录": recs},
                         ensure_ascii=False)
    return payload, total


def _fingerprint(session: Session, entity_id: int) -> tuple[int, int]:
    """(count, max_id) — max_id moves whenever rows are added, so delete+re-add is detected too."""
    row = session.execute(
        select(func.count(), func.coalesce(func.max(EntityTimeline.id), 0))
        .where(EntityTimeline.entity_id == entity_id)
    ).first()
    return int(row[0] or 0), int(row[1] or 0)


def _fp_key(count: int, max_id: int) -> int:
    return count * 1_000_003 + max_id


def needs_refresh(session: Session, e: Entity) -> bool:
    count, max_id = _fingerprint(session, e.id)
    if count == 0:
        return False
    if e.brief is None or (e.brief or {}).get("_v") != PROMPT_VERSION:
        return True                                   # never written, or written by an older prompt
    return _fp_key(count, max_id) != (e.brief_source_count or 0)


def refresh_entity(session: Session, e: Entity, model: str | None = None) -> bool:
    count, max_id = _fingerprint(session, e.id)
    if count == 0:
        return False
    payload, _total = _timeline_payload(session, e)
    out, meta = structured(session, EntityBriefOut, system=SYSTEM, user=payload,
                           model=model or model_enrich(), max_tokens=1200)
    e.brief = {**out.model_dump(), "_v": PROMPT_VERSION}
    e.brief_at = datetime.now(timezone.utc)
    e.brief_model = meta["model"]
    e.brief_source_count = _fp_key(count, max_id)
    return True


def refresh_all(session: Session, force: bool = False, limit: int = 20) -> dict:
    """Refresh briefs for entities whose timeline changed. Returns {updated, skipped, cost, errors}."""
    st = {"updated": 0, "skipped": 0, "cost": 0.0, "errors": []}
    if not is_configured():
        st["errors"].append("LLM not configured")
        return st
    before = _usage(session)
    for e in session.scalars(select(Entity).where(Entity.status == "active")).all():
        if st["updated"] >= limit:
            break
        if not (force or needs_refresh(session, e)):
            st["skipped"] += 1
            continue
        try:
            # nested: a failure here rolls back only this entity, never the caller's pending work
            # (sync_entities/update_timeline run in the same session) nor the recorded LLM spend
            with session.begin_nested():
                updated = refresh_entity(session, e)
            if updated:
                st["updated"] += 1
        except BudgetExceeded as ex:
            st["errors"].append(str(ex))
            break
        except Exception as ex:  # noqa: BLE001
            log.exception("brief failed for %s", e.canonical_name)
            st["errors"].append(f"{e.canonical_name}: {str(ex)[:120]}")
    st["cost"] = round(_usage(session) - before, 5)
    return st


def _usage(session: Session) -> float:
    from ..llm.client import today_usage
    return float(today_usage(session).cost_usd or 0)
