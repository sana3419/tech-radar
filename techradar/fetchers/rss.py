"""Generic RSS/Atom feeds. Config: {"feeds":[{"id":..,"url":..,"label":..}]}. Source name per feed: rss:<id>."""
from __future__ import annotations

import calendar
import re

_NIGHTLY_RE = re.compile(r"^b\d+$|nightly|-rc\d*$|rc\d+$|\bdev\b|snapshot", re.I)
from datetime import datetime, timezone
from typing import Iterable

import feedparser

from .base import BaseFetcher, RawItem, register


@register
class RSSFetcher(BaseFetcher):
    name = "rss"
    min_interval_s = 1.0

    def _feeds(self) -> list[dict]:
        feeds = list(self.config.get("feeds") or [])
        if self.config.get("entity_releases"):
            from ..settings import get_subscriptions
            for e in get_subscriptions().entities:
                gh = (e.anchors or {}).get("github")
                if gh and "/" in gh:
                    owner, repo = gh.split("/", 1)
                    feeds.append({"id": f"gh_release_{owner}_{repo}".replace("-", "_").replace(".", "_"),
                                  "url": f"https://github.com/{owner}/{repo}/releases.atom",
                                  "label": e.name, "limit": 5, "kind": "release", "entity": e.name})
        return feeds

    def fetch(self) -> Iterable[RawItem]:
        feeds = self._feeds()
        for f in feeds:
            fid, url = f["id"], f["url"]
            try:
                r = self.get(url)
            except Exception as e:  # one bad feed must not kill the others
                self.config.setdefault("_errors", []).append(f"rss:{fid}: {type(e).__name__}: {str(e)[:120]}")
                continue
            self.config.setdefault("_ok", []).append(f"rss:{fid}")
            parsed = feedparser.parse(r.content)
            for e in parsed.entries[: int(f.get("limit", 50))]:
                link = e.get("link")
                title = (e.get("title") or "").strip()
                if not link or not title:
                    continue
                if f.get("kind") == "release" and _NIGHTLY_RE.search(title):
                    continue   # skip nightly builds / release candidates
                pub = None
                for k in ("published_parsed", "updated_parsed"):
                    if e.get(k):
                        pub = datetime.fromtimestamp(calendar.timegm(e[k]), tz=timezone.utc)
                        break
                content = None
                if e.get("content"):
                    content = e["content"][0].get("value")
                elif e.get("summary"):
                    content = e["summary"]
                yield RawItem(
                    source=f"rss:{fid}",
                    external_id=e.get("id") or link,
                    title=(f"{f['entity']} {title}" if f.get("entity") and f["entity"].lower() not in title.lower() else title),
                    url=link,
                    source_url=link,
                    kind=f.get("kind", "article"),
                    author=(e.get("author") or None),
                    author_key=(e.get("author") or "").lower() or None,
                    published_at=pub,
                    metrics={},
                    content=content,
                    content_level=1 if content else 0,
                    tags_hint=[t.get("term") for t in e.get("tags", []) if t.get("term")][:10],
                    raw={"feed": fid, "label": f.get("label")},
                )
