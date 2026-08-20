"""Topic MOCs: one map-of-content page per subscribed topic, with an agent-written weekly narrative.

Entities answer "what is X doing"; topics answer "what happened in this area" — the layer that turns
scattered items into a theme. Written into the vault and rendered on the web config page.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..llm.client import BudgetExceeded, is_configured, model_enrich, structured
from ..llm.schemas import TopicMocOut
from ..models import Item
from ..settings import get_subscriptions

log = logging.getLogger(__name__)
PROMPT_VERSION = "moc-v1"
DAYS = 7
MAX_ITEMS = 30

SYSTEM = """你在为一位后端/AI 方向的开发者维护某个技术主题的「本周地图」。根据本周该主题下收集到的条目，写一段综述。
要求：
- 中文；只依据给定条目，不编造。
- summary：本周这个主题发生了什么、有什么变化或走向，≤120 字。要有观点，不要罗列标题。
- themes：1-4 条本周浮现的子主题/趋势，每条 ≤25 字。
- notable：0-3 条最值得点开的条目，格式「#id 一句话理由」，理由要说清为什么值得看。
- 条目很少时如实说明本周该主题安静，不要硬凑。"""


def topic_items(session: Session, topic_name: str, days: int = DAYS) -> list[Item]:
    since = datetime.now(timezone.utc) - timedelta(days=days)
    rows = session.scalars(
        select(Item).options(selectinload(Item.sources))
        # JSONB containment: matches only objects whose *name* is this topic (a substring LIKE would
        # also hit sibling topics' query/label values), and can use a jsonb_path_ops GIN index.
        .where(Item.first_seen_at >= since, Item.status != "filtered",
               Item.score_breakdown.contains({"hits": {"topics": [{"name": topic_name}]}}))
        .order_by(Item.score.desc().nullslast()).limit(MAX_ITEMS)
    ).all()
    return list(rows)


def build_moc(session: Session, topic, items: list[Item]) -> dict:
    """Returns {topic, label, count, items, narrative|None}."""
    out = {"topic": topic.name, "label": topic.label or topic.name, "queries": topic.queries,
           "count": len(items), "items": [], "narrative": None}
    for it in items:
        out["items"].append({
            "id": it.id, "title": it.title, "summary": it.summary_one, "url": it.url,
            "kind": it.kind, "date": (it.published_at or it.first_seen_at).isoformat()[:10],
            "entities": it.entities_matched or [],
        })
    if not items or not is_configured():
        return out
    payload = json.dumps({"主题": out["label"], "本周条目": [
        {"id": i["id"], "date": i["date"], "type": i["kind"], "text": (i["summary"] or i["title"])[:110]}
        for i in out["items"]]}, ensure_ascii=False)
    try:
        res, _meta = structured(session, TopicMocOut, system=SYSTEM, user=payload,
                                model=model_enrich(), max_tokens=1200)
        out["narrative"] = res.model_dump()
    except BudgetExceeded:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("moc narrative failed for %s", topic.name)
        out["narrative"] = None
    return out


def build_all(session: Session, days: int = DAYS) -> tuple[list[dict], bool]:
    """Returns (mocs, complete). `complete=False` when the run was cut short — callers must not use
    a partial list to decide which topic pages are orphans."""
    mocs, complete = [], True
    for t in get_subscriptions().topics:
        items = topic_items(session, t.name, days)
        try:
            mocs.append(build_moc(session, t, items))
        except BudgetExceeded as e:
            log.warning("moc stopped: %s", e)
            complete = False
            break
    return mocs, complete
