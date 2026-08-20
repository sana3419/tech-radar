from __future__ import annotations

from datetime import date

from sqlalchemy.orm import Session

from ..models import LlmUsage


def usage(session: Session, day: date | None = None) -> dict:
    if day is None:
        from ..llm.client import _local_today
        day = _local_today()
    u = session.get(LlmUsage, day)
    return {"day": day.isoformat(), "calls": u.calls if u else 0, "tokens_in": u.tokens_in if u else 0,
            "tokens_out": u.tokens_out if u else 0, "cost_usd": float(u.cost_usd) if u else 0.0}
