"""Hacker News via Algolia search API (no key). Fetches recent front-page-worthy stories."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from .base import BaseFetcher, RawItem, register

ALGOLIA = "https://hn.algolia.com/api/v1/search"


@register
class HackerNewsFetcher(BaseFetcher):
    name = "hackernews"
    min_interval_s = 0.5
    month_budget = 20000

    def fetch(self) -> Iterable[RawItem]:
        min_points = int(self.config.get("min_points", 30))
        hours = int(self.config.get("window_hours", 36))
        since = int((datetime.now(timezone.utc) - timedelta(hours=hours)).timestamp())
        pages = int(self.config.get("pages", 3))
        for page in range(pages):
            data = self.get_json(
                ALGOLIA,
                params={
                    "tags": "story",
                    "numericFilters": f"created_at_i>{since},points>={min_points}",
                    "hitsPerPage": 100,
                    "page": page,
                },
            )
            hits = data.get("hits", [])
            for h in hits:
                oid = str(h["objectID"])
                hn_url = f"https://news.ycombinator.com/item?id={oid}"
                url = h.get("url") or hn_url
                title = (h.get("title") or "").strip()
                if not title:
                    continue
                yield RawItem(
                    source=self.name,
                    external_id=oid,
                    title=title,
                    url=url,
                    source_url=hn_url,
                    kind="post" if not h.get("url") else "article",
                    author=h.get("author"),
                    author_key=(h.get("author") or "").lower() or None,
                    published_at=datetime.fromtimestamp(h["created_at_i"], tz=timezone.utc),
                    metrics={"points": h.get("points") or 0, "comments": h.get("num_comments") or 0},
                    content=(h.get("story_text") or None),
                    content_level=1 if h.get("story_text") else 0,
                    lang="en",
                    raw={k: h.get(k) for k in ("points", "num_comments", "author", "url", "created_at")},
                )
            if len(hits) < 100:
                break

    def refresh_metrics(self, external_ids: list[str]) -> dict[str, dict]:
        """Algolia supports filtering by objectID via tags=story_<id>; batch 50 per call."""
        out: dict[str, dict] = {}
        for i in range(0, len(external_ids), 50):
            chunk = external_ids[i:i + 50]
            tags = "story,(" + ",".join(f"story_{x}" for x in chunk) + ")"
            data = self.get_json(ALGOLIA, params={"tags": tags, "hitsPerPage": len(chunk)})
            for h in data.get("hits", []):
                out[str(h["objectID"])] = {"points": h.get("points") or 0, "comments": h.get("num_comments") or 0}
        return out
