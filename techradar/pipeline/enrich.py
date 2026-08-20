"""Enrich job: batch structured summaries/tags for queued+scored items lacking enrich_model."""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..llm.client import BudgetExceeded, LLMNotConfigured, is_configured, model_enrich, structured
from ..llm.schemas import EnrichBatchOut
from ..models import AgentTask, Item
from ..settings import get_subscriptions, get_taxonomy

log = logging.getLogger(__name__)
PROMPT_VERSION = "enrich-v1"
BATCH = 15


@dataclass
class EnrichStats:
    batches: int = 0
    items: int = 0
    cost: float = 0.0
    errors: list[str] = field(default_factory=list)
    stopped_reason: str | None = None


def _system_prompt() -> str:
    tax = get_taxonomy()
    tpl = (Path(__file__).parent.parent / "llm/prompts/enrich_system.md").read_text(encoding="utf-8")
    return tpl.format(types=", ".join(tax.get("types", [])), domains=", ".join(tax.get("domains", [])),
                      stacks=", ".join(tax.get("stacks", [])))


def _payload(items: list[Item]) -> str:
    cand = [e.name for e in get_subscriptions().entities]
    rows = []
    for i, it in enumerate(items):
        rows.append({
            "idx": i, "title": it.title, "url": it.url,
            "source": ",".join(sorted({s.source.split(":")[0] for s in it.sources})),
            "content": (it.content or "")[:1500],
        })
    return json.dumps({"候选实体": cand, "items": rows}, ensure_ascii=False)


def _apply(item: Item, out, meta: dict) -> None:
    tax = get_taxonomy()
    item.summary_one = out.summary_one[:120]
    item.summary_points = [p[:80] for p in out.points][:3]
    item.tags = {
        "type": out.type if out.type in tax.get("types", []) else "other",
        "domains": [d for d in out.domains if d in tax.get("domains", [])][:3],
        "stacks": [s for s in out.stacks if s in tax.get("stacks", [])][:3],
    }
    cand = {e.name for e in get_subscriptions().entities}
    ents = [e for e in out.entities if e in cand]
    if ents:
        item.entities_matched = sorted(set((item.entities_matched or []) + ents))
    if out.lang in ("zh", "en"):
        item.lang = out.lang
    item.enrich_model = meta["model"]
    item.enrich_version = PROMPT_VERSION
    if item.status == "queued":
        item.status = "enriched"


def pending_items(session: Session, hours: int = 72, limit: int = 200) -> list[Item]:
    now = datetime.now(timezone.utc)
    return list(session.scalars(
        select(Item).options(selectinload(Item.sources))
        .where(Item.status.in_(("queued", "scored", "digested")), Item.enrich_model.is_(None),
               Item.first_seen_at > now - timedelta(hours=hours))
        .order_by(Item.score.desc().nullslast(), Item.first_seen_at.desc()).limit(limit)
    ).all())


def run_enrich_items(session: Session, item_ids: list[int], model: str | None = None) -> EnrichStats:
    """Enrich specific items (e.g. digest candidates) regardless of status."""
    items = list(session.scalars(
        select(Item).options(selectinload(Item.sources)).where(Item.id.in_(item_ids), Item.enrich_model.is_(None))
    ).all())
    return _enrich_batches(session, items, batch=BATCH, model=model)


def run_enrich(session: Session, limit: int = 200, batch: int = BATCH, model: str | None = None) -> EnrichStats:
    st = EnrichStats()
    from ..llm.client import budget_remaining
    from ..settings import get_settings
    if not is_configured():
        st.stopped_reason = "LLM not configured (set TECHRADAR_OPENAI_API_KEY)"
        return st
    items = pending_items(session, limit=limit)
    # degrade: when budget is low (<30%), only enrich subscription hits
    if budget_remaining(session) < get_settings().llm_daily_budget_usd * 0.3:
        items = [it for it in items if (it.score_breakdown or {}).get("sub_hit", 0) > 0]
        st.stopped_reason = "low budget: subscription hits only"
    r = _enrich_batches(session, items, batch=batch, model=model)
    r.stopped_reason = r.stopped_reason or st.stopped_reason
    return r


def _enrich_batches(session: Session, items: list[Item], batch: int, model: str | None) -> EnrichStats:
    st = EnrichStats()
    model = model or model_enrich()
    if not is_configured():
        st.stopped_reason = "LLM not configured (set TECHRADAR_OPENAI_API_KEY)"
        return st
    if not items:
        return st
    system = _system_prompt()
    for i in range(0, len(items), batch):
        chunk = items[i:i + batch]
        task = AgentTask(type="enrich_batch", payload={"item_ids": [x.id for x in chunk]}, status="running",
                         model=model, prompt_version=PROMPT_VERSION)
        session.add(task)
        session.flush()
        try:
            out, meta = structured(session, EnrichBatchOut, system=system, user=_payload(chunk),
                                   model=model, max_tokens=6000)
            if len(out.items) != len(chunk):
                raise RuntimeError(f"count mismatch {len(out.items)} != {len(chunk)}")
            for it, o in zip(chunk, out.items):
                _apply(it, o, meta)
            task.status, task.tokens_in, task.tokens_out, task.cost_usd = "done", meta["tokens_in"], meta["tokens_out"], meta["cost"]
            task.finished_at = datetime.now(timezone.utc)
            st.batches += 1
            st.items += len(chunk)
            st.cost += meta["cost"]
            session.commit()
        except BudgetExceeded as e:
            task.status, task.error = "failed", str(e)
            st.stopped_reason = str(e)
            session.commit()
            break
        except Exception as e:  # noqa: BLE001
            log.exception("enrich batch failed")
            task.status, task.error, task.attempts = "failed", str(e)[:500], (task.attempts or 0) + 1
            st.errors.append(str(e)[:200])
            session.commit()
            name = type(e).__name__
            if isinstance(e, (LLMNotConfigured, TypeError)) or name in ("AuthenticationError", "PermissionDeniedError"):
                st.stopped_reason = f"fatal: {name}"
                break
    return st
