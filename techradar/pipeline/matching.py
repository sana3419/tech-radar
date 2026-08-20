"""Rule matching shared by filter & score: subscription topics, author whitelist, entity aliases."""
from __future__ import annotations

import re
from dataclasses import dataclass, field

from ..models import Item
from ..settings import get_subscriptions


@dataclass
class RuleHits:
    topics: list[dict] = field(default_factory=list)    # {"name","label","boost","query"}
    authors: list[dict] = field(default_factory=list)   # {"source","key","weight"}
    entities: list[str] = field(default_factory=list)   # canonical names

    @property
    def any(self) -> bool:
        return bool(self.topics or self.authors or self.entities)

    @property
    def max_boost(self) -> float:
        return max([t["boost"] for t in self.topics] + [0.0])

    @property
    def max_author_weight(self) -> float:
        return max([a["weight"] for a in self.authors] + [0.0])

    def to_json(self) -> dict:
        return {"topics": self.topics, "authors": self.authors, "entities": self.entities}


def _text_of(item: Item) -> str:
    parts = [item.title or "", item.content or "", item.url or ""]
    for s in item.sources:
        hints = (s.raw or {}).get("tags_hint") or []
        parts.extend(hints)
    return " ".join(parts).lower()


def _query_matches(q: str, text: str) -> bool:
    q = q.lower().strip()
    if not q:
        return False
    if " " in q:
        return q in text
    # single token: word-boundary-ish (allow . / - inside like llama.cpp, vllm-project)
    return re.search(rf"(?<![a-z0-9]){re.escape(q)}(?![a-z0-9])", text) is not None


def match_item(item: Item) -> RuleHits:
    subs = get_subscriptions()
    text = _text_of(item)
    hits = RuleHits()
    item_sources = {s.source.split(":")[0] for s in item.sources}
    for t in subs.topics:
        if t.sources and not (set(t.sources) & item_sources):
            continue
        for q in t.queries:
            if _query_matches(q, text):
                hits.topics.append({"name": t.name, "label": t.label or t.name, "boost": t.boost, "query": q})
                break
    author_keys = {(s.source.split(":")[0], (s.author_key or "").lower()) for s in item.sources if s.author_key}
    for a in subs.authors:
        if (a.source, a.key.lower()) in author_keys:
            hits.authors.append({"source": a.source, "key": a.key, "weight": a.weight})
    for e in subs.entities:
        names = [e.name] + list(e.aliases) + [v for v in e.anchors.values()]
        if any(_query_matches(n, text) for n in names if n):
            hits.entities.append(e.name)
    return hits
