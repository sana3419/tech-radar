"""Weekly review (Sunday 20:00): what you saved, what kept rising, watched-entity activity, stats."""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import Digest, Entity, EntityTimeline, Feedback, Item, LlmUsage
from ..services.entities import entity_overview
from .daily import _esc, _intro, _link, _srcs, local_today

WEEK_DAYS = 7


def _week_range(day: date) -> tuple[datetime, datetime]:
    from zoneinfo import ZoneInfo
    from ..settings import get_settings
    tz = ZoneInfo(get_settings().timezone)
    end = datetime(day.year, day.month, day.day, tzinfo=tz) + timedelta(days=1)
    return (end - timedelta(days=WEEK_DAYS)).astimezone(timezone.utc), end.astimezone(timezone.utc)


def build_weekly(session: Session, day: date | None = None) -> dict:
    day = day or local_today()
    since, until = _week_range(day)
    # 1. saved this week
    saved_rows = session.execute(
        select(Feedback.item_id, func.min(Feedback.ts)).where(Feedback.action == "save", Feedback.ts >= since)
        .group_by(Feedback.item_id)
    ).all()
    saved = [session.scalar(select(Item).options(selectinload(Item.sources)).where(Item.id == iid))
             for iid, _ in saved_rows]
    saved = [x for x in saved if x][:15]
    # 2. kept rising: items seen this week appearing in ≥2 sources or with events, by score
    rising = list(session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.first_seen_at >= since, Item.score.isnot(None), Item.status != "filtered",
               Item.kind != "release")
        .order_by(Item.score.desc()).limit(100)
    ).all())
    # union source *families* across each event group; require ≥2 distinct families
    from ..models import ItemSource
    fam = lambda src: src.split(":")[0]  # noqa: E731
    groups: dict[int, list[Item]] = {}
    for it in rising:
        groups.setdefault(it.event_id or it.id, []).append(it)
    multi = []
    rising_sources: dict[int, set] = {}
    for gid, members in groups.items():
        fams = {fam(s.source) for m in members for s in m.sources}
        if len(fams) < 2 and members[0].event_id:
            # sampled members may miss low-scored siblings — check the whole event group in DB
            rows = session.execute(
                select(ItemSource.source).join(Item, Item.id == ItemSource.item_id)
                .where(Item.event_id == members[0].event_id)
            ).all()
            fams = {fam(r[0]) for r in rows}
        if len(fams) >= 2:
            keeper = members[0]        # best-scored member (rising is score-desc)
            multi.append(keeper)
            if keeper.event_id:
                rows = session.execute(
                    select(ItemSource.source).join(Item, Item.id == ItemSource.item_id)
                    .where(Item.event_id == keeper.event_id, Item.id != keeper.id)
                ).all()
                rising_sources[keeper.id] = {r[0] for r in rows}
        if len(multi) >= 8:
            break
    # 3. watched entities activity
    ents = []
    for e in session.scalars(select(Entity).where(Entity.watched.is_(True))).all():
        cnt = session.scalar(
            select(func.count()).select_from(EntityTimeline)
            .where(EntityTimeline.entity_id == e.id, EntityTimeline.ts >= since)
        )
        if cnt:
            ov = entity_overview(session, e, limit=3)
            ents.append({"name": e.canonical_name, "count": cnt, "recent": ov["timeline"][:3]})
    # 4. stats
    n_items = session.scalar(select(func.count()).select_from(Item).where(Item.first_seen_at >= since))
    n_digested = session.scalar(select(func.count()).select_from(Item)
                                .where(Item.first_seen_at >= since, Item.status == "digested"))
    fb = dict(session.execute(
        select(Feedback.action, func.count()).where(Feedback.ts >= since).group_by(Feedback.action)
    ).all())
    cost = session.scalar(select(func.sum(LlmUsage.cost_usd)).where(LlmUsage.day >= since.date())) or 0
    return {"day": day, "saved": saved, "rising": multi, "rising_sources": rising_sources, "entities": ents,
            "stats": {"items": n_items, "digested": n_digested, "feedback": fb, "cost": float(cost)}}


def render_weekly(w: dict, html: bool = False) -> str:
    day = w["day"]
    lines = [f"📚 本周回顾 · {day.isoformat()}", ""]
    if w["saved"]:
        lines.append("⭐ 你收藏了：")
        for it in w["saved"]:
            lines.append(f"· {_link(_intro(it), it.url, html)} [{_srcs(it)}]")
        lines.append("")
    if w["rising"]:
        lines.append("📈 本周多源热议：")
        for it in w["rising"]:
            extra = w.get("rising_sources", {}).get(it.id)
            lines.append(f"· {_link(_intro(it), it.url, html)} [{_srcs(it, extra)}]")
        lines.append("")
    if w["entities"]:
        lines.append("👀 关注实体动态：")
        for e in w["entities"]:
            lines.append(f"· {_esc(e['name'], html)}：{e['count']} 条新记录，最近—" +
                         "；".join((t["summary"] or t["title"])[:40] for t in e["recent"]))
        lines.append("")
    st = w["stats"]
    fb = st["feedback"]
    lines.append(f"📊 本周入库 {st['items']} 条 · 推送 {st['digested']} 条 · "
                 f"收藏 {fb.get('save', 0)} · 忽略 {fb.get('ignore', 0)} · 深挖 {fb.get('dig', 0)} · "
                 f"LLM 花费 ${st['cost']:.2f}")
    return "\n".join(lines)


def persist_weekly(session: Session, w: dict, markdown: str, sent: bool) -> Digest:
    dg = session.scalar(select(Digest).where(Digest.day == w["day"], Digest.kind == "weekly"))
    if dg is None:
        dg = Digest(day=w["day"], kind="weekly")
        session.add(dg)
        session.flush()
    dg.markdown = markdown
    dg.stats = w["stats"] | {"saved": len(w["saved"]), "rising": len(w["rising"])}
    if sent:
        dg.sent_at = datetime.now(timezone.utc)
    return dg
