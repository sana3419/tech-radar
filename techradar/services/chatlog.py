"""Persisted Q&A turns (agent_tasks type='chat') so /ask has history and answers can be saved."""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import AgentTask


def record_turn(session: Session, question: str, result: dict) -> AgentTask:
    t = AgentTask(type="chat", payload={"question": question}, status="done",
                  result={"answer": result["answer"], "citations": result["citations"],
                          "web_count": result.get("web_count", 0)},
                  cost_usd=result.get("cost") or 0, finished_at=datetime.now(timezone.utc))
    session.add(t)
    session.flush()
    return t


def recent_turns(session: Session, limit: int = 20, within_minutes: int | None = None) -> list[dict]:
    """within_minutes: only turns from the current conversation window (used for follow-up context,
    so a question asked days ago never becomes 'the previous turn')."""
    from datetime import timedelta
    q = select(AgentTask).where(AgentTask.type == "chat")
    if within_minutes:
        q = q.where(AgentTask.created_at >= datetime.now(timezone.utc) - timedelta(minutes=within_minutes))
    rows = session.scalars(q.order_by(AgentTask.id.desc()).limit(limit)).all()
    out = [{
        "id": t.id,
        "q": (t.payload or {}).get("question", ""),
        "a": (t.result or {}).get("answer", ""),
        "citations": (t.result or {}).get("citations", []),
        "at": t.created_at.isoformat()[:16] if t.created_at else "",
        "cost": float(t.cost_usd or 0),
        "saved": bool((t.result or {}).get("note_path")),
        "web_count": (t.result or {}).get("web_count", 0),
    } for t in rows]
    out.reverse()          # oldest first, like a chat transcript
    return out


def get_turn(session: Session, task_id: int) -> AgentTask | None:
    t = session.get(AgentTask, task_id)
    return t if t and t.type == "chat" else None


def clear_turns(session: Session) -> int:
    rows = session.scalars(select(AgentTask).where(AgentTask.type == "chat")).all()
    for r in rows:
        session.delete(r)
    return len(rows)
