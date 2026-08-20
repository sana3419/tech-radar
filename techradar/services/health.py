from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import SourceHealth


def list_sources_health(session: Session) -> list[dict]:
    rows = session.scalars(select(SourceHealth).order_by(SourceHealth.source)).all()
    return [
        {
            "source": r.source,
            "last_run_at": r.last_run_at.isoformat() if r.last_run_at else None,
            "last_success_at": r.last_success_at.isoformat() if r.last_success_at else None,
            "last_items": r.last_items,
            "consecutive_failures": r.consecutive_failures,
            "last_error": r.last_error,
            "month_calls": r.month_calls,
            "month_budget": r.month_budget,
        }
        for r in rows
    ]


def alerts(session: Session, threshold: int = 2) -> list[str]:
    return [
        f"{r.source} 连续 {r.consecutive_failures} 次失败: {(r.last_error or '')[:80]}"
        for r in session.scalars(select(SourceHealth)).all()
        if (r.consecutive_failures or 0) >= threshold
    ]
