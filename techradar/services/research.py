"""Research task queue (P1 agent loop lives in agents/research.py; enqueue is P0 so buttons work)."""
from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentTask


def enqueue_research(session: Session, item_id: int | None = None, entity_id: int | None = None,
                     question: str | None = None) -> dict:
    payload = {"item_id": item_id, "entity_id": entity_id, "question": question}
    existing = session.scalar(
        select(AgentTask).where(AgentTask.type == "research", AgentTask.status.in_(("pending", "running")),
                                AgentTask.payload["item_id"].as_integer() == (item_id or -1))
    ) if item_id else None
    if existing:
        return {"task_id": existing.id, "status": existing.status, "dedup": True}
    t = AgentTask(type="research", payload=payload, status="pending")
    session.add(t)
    session.flush()
    return {"task_id": t.id, "status": t.status}


def get_task(session: Session, task_id: int) -> dict | None:
    t = session.get(AgentTask, task_id)
    if not t:
        return None
    return {"id": t.id, "type": t.type, "status": t.status, "payload": t.payload, "result": t.result,
            "error": t.error, "cost_usd": float(t.cost_usd) if t.cost_usd else None,
            "created_at": t.created_at.isoformat() if t.created_at else None}
