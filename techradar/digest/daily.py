"""Daily digest: Top N + folded + recall + alerts + cost. Hard limits from settings (docs/01 §3.5)."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date, datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..models import Digest, DigestItem, Entity, Feedback, Item
from ..services.health import alerts as health_alerts
from ..services.usage import usage as llm_usage
from ..settings import get_settings

SRC_ABBR = {"hackernews": "HN", "github": "GH", "arxiv": "arXiv", "rss": "RSS", "x": "X", "wechat": "WX", "reddit": "RD"}
TYPE_EMOJI = {"release": "🚀", "tool": "🧰", "paper": "🧠", "opinion": "💬", "tutorial": "📘", "incident": "🚨", "other": "📌"}


@dataclass
class DigestData:
    day: date
    top: list[Item] = field(default_factory=list)
    folded: list[Item] = field(default_factory=list)
    recall: list[tuple[str, int]] = field(default_factory=list)   # (entity, n_new)
    event_sources: dict = field(default_factory=dict)             # item_id -> extra sources from folded siblings
    alerts: list[str] = field(default_factory=list)
    cost_usd: float = 0.0
    explore: set[int] = field(default_factory=set)


def _family(it: Item) -> str:
    """Source family for diversity: hackernews/github/rss:<feed>."""
    srcs = sorted({x.source for x in it.sources})
    return srcs[0] if srcs else "unknown"


from functools import lru_cache


@lru_cache(maxsize=1)
def _feed_labels() -> dict[str, str]:
    from ..fetchers.rss import RSSFetcher
    from ..settings import get_subscriptions
    feeds = RSSFetcher(get_subscriptions().sources.get("rss") or {})._feeds()
    return {f"rss:{f['id']}": (f.get("label") or f["id"])[:14] for f in feeds}


def _srcs(it: Item, extra: set | None = None) -> str:
    labels = _feed_labels()
    keys = set()
    names = [s.source for s in it.sources] + sorted(extra or [])
    for src in names:
        if src in labels:
            keys.add(labels[src])
        else:
            k = src.split(":")[0]
            keys.add(SRC_ABBR.get(k, k))
    return "+".join(sorted(keys))


def _emoji(it: Item) -> str:
    t = (it.tags or {}).get("type") if it.tags else None
    if t:
        return TYPE_EMOJI.get(t, "📌")
    return {"repo": "🧰", "paper": "🧠", "release": "🚀"}.get(it.kind, "📌")


def _already_delivered(session: Session, day: date, days: int = 7) -> set[int]:
    """Items shown in *previous* days' digests (today's own digest is excluded so a same-day rebuild
    reproduces the same set instead of a fresh one). Extended to event siblings: once an event was
    delivered, later-arriving members of the same event are also considered delivered."""
    since = day - timedelta(days=days)
    rows = session.execute(
        select(DigestItem.item_id).join(Digest, Digest.id == DigestItem.digest_id)
        .where(Digest.day >= since, Digest.day < day, DigestItem.section.in_(("top", "folded")))
    ).all()
    ids = {r[0] for r in rows}
    if ids:
        ev = session.execute(select(Item.event_id).where(Item.id.in_(ids), Item.event_id.isnot(None))).all()
        eids = {r[0] for r in ev}
        if eids:
            sib = session.execute(select(Item.id).where(Item.event_id.in_(eids))).all()
            ids |= {r[0] for r in sib}
    return ids


def local_today() -> date:
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def select_digest(session: Session, day: date | None = None) -> DigestData:
    s = get_settings()
    day = day or local_today()
    d = DigestData(day=day)
    delivered = _already_delivered(session, day)
    acted = {r[0] for r in session.execute(select(Feedback.item_id).where(Feedback.action.in_(("ignore", "read", "save")))).all()}
    cands = session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.status.in_(("scored", "digested", "enriched")), Item.score.isnot(None))
        .order_by(Item.score.desc())
        .limit(200)
    ).all()
    cands = [c for c in cands if c.id not in delivered and c.id not in acted and (c.score or 0) > 0]
    # event folding: keep only the best-scored member of each event; remember siblings for the source badge
    seen_events: dict[int, Item] = {}
    folded_out: list[Item] = []
    d.event_sources = {}
    for c in cands:                      # cands are score-desc already
        if c.event_id:
            if c.event_id in seen_events:
                keeper = seen_events[c.event_id]
                d.event_sources.setdefault(keeper.id, set()).update(x.source for x in c.sources)
                continue
            seen_events[c.event_id] = c
        folded_out.append(c)
    cands = folded_out
    top_n, fold_n = s.digest_top_n, s.digest_folded_n
    # diversity caps inside Top: papers ≤ 3, same source family ≤ 4
    KIND_CAP = {"paper": 3, "release": 2}
    SRC_CAP = 4
    kind_cnt: dict[str, int] = {}
    src_cnt: dict[str, int] = {}
    seen_release_repo: set[str] = set()

    def _repo(c: Item) -> str:
        return c.canonical_key.split("#")[0] if c.canonical_key.startswith("gh:") else c.canonical_key

    def _fits(c: Item) -> bool:
        fam = _family(c)
        if kind_cnt.get(c.kind, 0) >= KIND_CAP.get(c.kind, 99):
            return False
        if src_cnt.get(fam, 0) >= SRC_CAP:
            return False
        if c.kind == "release" and _repo(c) in seen_release_repo:   # one release per repo per digest
            return False
        return True

    def _take(c: Item):
        kind_cnt[c.kind] = kind_cnt.get(c.kind, 0) + 1
        fam = _family(c)
        src_cnt[fam] = src_cnt.get(fam, 0) + 1
        if c.kind == "release":
            seen_release_repo.add(_repo(c))
        top.append(c)

    sub_hits = [c for c in cands if (c.score_breakdown or {}).get("sub_hit", 0) > 0]
    non_sub = [c for c in cands if (c.score_breakdown or {}).get("sub_hit", 0) == 0]
    top: list[Item] = []
    reserve = 1 if non_sub else 0            # explore quota
    for c in sub_hits:
        if len(top) >= top_n - reserve:
            break
        if _fits(c):
            _take(c)
    if non_sub and len(top) < top_n:
        pick = next((c for c in non_sub if _fits(c)), None)
        if pick is None:   # source-family cap may exclude all; explore slot ignores it (kind caps still apply)
            pick = next((c for c in non_sub if kind_cnt.get(c.kind, 0) < KIND_CAP.get(c.kind, 99)
                         and not (c.kind == "release" and _repo(c) in seen_release_repo)), None)
        if pick is not None:
            _take(pick)
            d.explore.add(pick.id)
    if len(top) < top_n:
        for c in cands:
            if len(top) >= top_n:
                break
            if c not in top and _fits(c):
                _take(c)
    if len(top) < top_n:                      # relax caps if still short
        for c in cands:
            if len(top) >= top_n:
                break
            if c not in top:
                top.append(c)
    top_ids = {t.id for t in top}
    d.top = sorted(top, key=lambda x: -(x.score or 0))
    folded: list[Item] = []
    for c in cands:
        if c.id in top_ids:
            continue
        if c.kind == "release":
            if _repo(c) in seen_release_repo:
                continue
            seen_release_repo.add(_repo(c))
        folded.append(c)
        if len(folded) >= fold_n:
            break
    d.folded = folded
    # recall: watched entities with new items in last 24h
    watched = session.scalars(select(Entity).where(Entity.watched.is_(True))).all()
    if watched:
        since = datetime.now(timezone.utc) - timedelta(hours=24)
        for e in watched:
            n = session.scalar(
                select(Item.id).where(Item.first_seen_at > since, Item.entities_matched.contains([e.canonical_name])).limit(1)
            )
            if n:
                cnt = len(session.scalars(select(Item.id).where(Item.first_seen_at > since, Item.entities_matched.contains([e.canonical_name]))).all())
                d.recall.append((e.canonical_name, cnt))
    d.alerts = health_alerts(session)
    d.cost_usd = llm_usage(session, day)["cost_usd"]
    return d


def _link(title: str, url: str, html: bool) -> str:
    import html as _h
    if html:
        return f'<a href="{_h.escape(url, quote=True)}">{_h.escape(title)}</a>'
    return f"[{title}]({url})"


def _esc(text: str, html: bool) -> str:
    import html as _h
    return _h.escape(text) if html else text


def render_markdown(d: DigestData, web_base: str | None = None, html: bool = False) -> str:
    """Render digest: continuous numbering (top 1..N, folded N+1..), title link + one-line intro only.
    html=True produces Telegram-safe HTML."""
    lines = [f"📅 {d.day.month}/{d.day.day} 技术日报", ""]
    n = 0
    for it in d.top:
        n += 1
        tag = " 🎲" if it.id in d.explore else ""
        lines.append(f"{n}. {_link(_intro(it), it.url, html)} [{_srcs(it, d.event_sources.get(it.id))}]{tag}")
    if d.folded:
        lines.append("")
        lines.append("📦 更多")
        for it in d.folded:
            n += 1
            lines.append(f"{n}. {_link(_intro(it), it.url, html)} [{_srcs(it, d.event_sources.get(it.id))}]")
    if d.recall:
        lines.append("")
        lines.append("🔁 回顾：" + "；".join(f"你关注的 {e} 有 {n} 条新动静" for e, n in d.recall))
    if d.alerts:
        lines.append("")
        lines.append("⚠️ 源告警：" + _esc("；".join(d.alerts), html))
    lines.append("")
    lines.append(f"💰 今日 LLM 花费 ${d.cost_usd:.3f}")
    return "\n".join(lines)


def _intro(it: Item) -> str:
    """Chinese one-liner used as the link text; falls back to the title when not summarized yet."""
    one = (it.summary_one or "").strip().rstrip("。")
    return one[:80] if one else it.title[:80]


def persist_digest(session: Session, d: DigestData, markdown: str, sent: bool) -> Digest:
    dg = session.scalar(select(Digest).where(Digest.day == d.day, Digest.kind == "daily"))
    if dg is None:
        dg = Digest(day=d.day, kind="daily")
        session.add(dg)
        session.flush()
    dg.markdown = markdown
    dg.stats = {"pushed": len(d.top), "folded": len(d.folded), "cost": d.cost_usd, "alerts": len(d.alerts),
                "explore": sorted(d.explore)}
    if sent:
        dg.sent_at = datetime.now(timezone.utc)
    # replace items
    for row in session.scalars(select(DigestItem).where(DigestItem.digest_id == dg.id)).all():
        session.delete(row)
    session.flush()
    for i, it in enumerate(d.top, 1):
        session.add(DigestItem(digest_id=dg.id, item_id=it.id, section="top", position=i))
        if it.status in ("scored", "enriched"):
            it.status = "digested"
    offset = len(d.top)
    for i, it in enumerate(d.folded, 1):
        session.add(DigestItem(digest_id=dg.id, item_id=it.id, section="folded", position=offset + i))
        if it.status in ("scored", "enriched"):
            it.status = "digested"
    return dg


def resolve_positions(session: Session, numbers: list[int], day: date | None = None) -> dict[int, int | None]:
    """Map digest numbers → item ids for the latest sent digest (today by default, else most recent)."""
    day = day or local_today()
    dg = session.scalar(select(Digest).where(Digest.day <= day, Digest.kind == "daily").order_by(Digest.day.desc()))
    out: dict[int, int | None] = {n: None for n in numbers}
    if not dg:
        return out
    rows = session.execute(select(DigestItem.position, DigestItem.item_id).where(DigestItem.digest_id == dg.id)).all()
    pos = {p: iid for p, iid in rows}
    for n in numbers:
        out[n] = pos.get(n)
    return out


def ensure_enriched(session: Session, d: DigestData) -> None:
    """Summarize digest items that lack a summary (small, bounded LLM call) so folded items get intros too."""
    from ..pipeline.enrich import run_enrich_items
    ids = [it.id for it in d.top + d.folded if not it.summary_one]
    if ids:
        run_enrich_items(session, ids)
        for it in d.top + d.folded:
            session.refresh(it)
    d.cost_usd = llm_usage(session, d.day)["cost_usd"]


def load_persisted(session: Session, day: date | None = None) -> DigestData | None:
    """Rebuild DigestData from digest_items so /today shows exactly what was sent (numbers stay valid)."""
    day = day or local_today()
    dg = session.scalar(select(Digest).where(Digest.day == day, Digest.kind == "daily"))
    if not dg:
        return None
    rows = session.execute(
        select(DigestItem.section, DigestItem.position, DigestItem.item_id)
        .where(DigestItem.digest_id == dg.id).order_by(DigestItem.position)
    ).all()
    if not rows:
        return None
    d = DigestData(day=day)
    for section, pos, iid in rows:
        it = session.scalar(select(Item).options(selectinload(Item.sources)).where(Item.id == iid))
        if not it:
            continue
        (d.top if section == "top" else d.folded).append(it)
    d.alerts = health_alerts(session)
    d.cost_usd = llm_usage(session, day)["cost_usd"]
    st = dg.stats or {}
    d.explore = set(st.get("explore") or [])
    # rebuild folded-event source badges (not persisted): union sibling sources per event
    for it in d.top + d.folded:
        if it.event_id:
            sibs = session.execute(
                select(Item).where(Item.event_id == it.event_id, Item.id != it.id)
            ).scalars().all()
            extra = {s.source for sib in sibs for s in sib.sources}
            if extra:
                d.event_sources[it.id] = extra
    return d
