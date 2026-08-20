"""Manual feed: /add <url> — fetch title, ingest as source 'manual', boost via feedback save."""
from __future__ import annotations

import re
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from ..fetchers.base import RawItem
from ..pipeline.ingest import ingest_raw
from .feedback import record_feedback


def _title_of(url: str) -> str:
    try:
        r = httpx.get(url, timeout=10, follow_redirects=True, headers={"User-Agent": "techradar/0.1"})
        m = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.I | re.S)
        if m:
            return re.sub(r"\s+", " ", m.group(1)).strip()[:200]
    except Exception:  # noqa: BLE001
        pass
    return url


def add_url(session: Session, url: str, note: str | None = None) -> dict:
    title = _title_of(url)
    raw = RawItem(source="manual", external_id=url, title=title, url=url, kind="other",
                  published_at=datetime.now(timezone.utc), metrics={})
    item, created, _ = ingest_raw(session, raw)
    if item.status == "new":
        item.status = "queued"
    session.flush()
    record_feedback(session, item.id, "save", channel="telegram", note=note)
    return {"id": item.id, "title": item.title, "created": created}
