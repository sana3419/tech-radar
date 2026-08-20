"""GitHub Search: recently created repos sorted by stars, per language."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Iterable

from ..settings import get_settings
from .base import BaseFetcher, RawItem, register

SEARCH = "https://api.github.com/search/repositories"
REPO_BY_ID = "https://api.github.com/repositories/{id}"


@register
class GitHubFetcher(BaseFetcher):
    name = "github"
    min_interval_s = 2.5     # search API: 30 req/min authenticated, 10 unauth
    month_budget = 5000

    def __init__(self, config=None):
        super().__init__(config)
        if not get_settings().github_token:
            self.min_interval_s = 6.5   # unauthenticated search: 10 req/min
            self._limiter.min_interval_s = 6.5

    def extra_headers(self):
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28"}
        tok = get_settings().github_token
        if tok:
            h["Authorization"] = f"Bearer {tok}"
        return h

    def fetch(self) -> Iterable[RawItem]:
        langs = self.config.get("languages") or [None]
        min_stars = int(self.config.get("min_stars", 50))
        window_days = int(self.config.get("window_days", 7))
        since = (datetime.now(timezone.utc) - timedelta(days=window_days)).date().isoformat()
        seen: set[str] = set()
        for lang in langs:
            q = f"created:>{since} stars:>={min_stars}"
            if lang:
                q += f" language:{lang}"
            data = self.get_json(SEARCH, params={"q": q, "sort": "stars", "order": "desc", "per_page": 50})
            for r in data.get("items", []):
                full = r["full_name"]
                if full in seen:
                    continue
                seen.add(full)
                owner = r["owner"]["login"]
                yield RawItem(
                    source=self.name,
                    external_id=str(r["id"]),
                    title=f"{full}: {r.get('description') or ''}".strip(": "),
                    url=r["html_url"],
                    source_url=r["html_url"],
                    kind="repo",
                    author=owner,
                    author_key=owner.lower(),
                    published_at=datetime.fromisoformat(r["created_at"].replace("Z", "+00:00")),
                    metrics={"stars": r.get("stargazers_count", 0), "forks": r.get("forks_count", 0)},
                    content=r.get("description"),
                    content_level=1 if r.get("description") else 0,
                    lang="en",
                    tags_hint=[t for t in (r.get("topics") or [])][:10] + ([r["language"].lower()] if r.get("language") else []),
                    raw={"language": r.get("language"), "topics": r.get("topics"), "pushed_at": r.get("pushed_at")},
                )

    def refresh_metrics(self, external_ids: list[str]) -> dict[str, dict]:
        """One call per repo (core API 5000/h with token). Caller limits batch size."""
        out: dict[str, dict] = {}
        self._limiter.min_interval_s = 0.2 if get_settings().github_token else 1.0
        for ext in external_ids:
            try:
                r = self.get_json(REPO_BY_ID.format(id=ext))
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "404" in msg:
                    out[ext] = {"stars": 0, "forks": 0, "gone": True}   # repo deleted/private: stop wondering
                else:
                    self.config.setdefault("_errors", []).append(f"github:{ext}: {msg[:80]}")
                    if "403" in msg or "429" in msg:
                        break   # rate limited: stop this round, retry next hour
                continue
            out[ext] = {"stars": r.get("stargazers_count", 0), "forks": r.get("forks_count", 0)}
        return out
