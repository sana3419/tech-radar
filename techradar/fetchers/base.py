"""BaseFetcher: every source implements `fetch()` and yields RawItem.

Built-in: rate limiting, exponential backoff, monthly call budget, health recording.
Fetchers must be idempotent and never raise out of `run()`; failures are recorded in source_health.
"""
from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Iterable

import httpx
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

log = logging.getLogger(__name__)


class RawItem(BaseModel):
    """Unified output of every fetcher (docs/02 §1)."""

    source: str                       # hackernews | github | rss:<feed_id> | ...
    external_id: str
    title: str
    url: str                          # the *thing* url (for HN: the linked article, not the HN page)
    source_url: str | None = None     # where it was seen (HN item page, tweet url, ...)
    kind: str = "other"               # repo | paper | article | release | post | other
    author: str | None = None
    author_key: str | None = None
    published_at: datetime | None = None
    metrics: dict[str, Any] = Field(default_factory=dict)   # {"points":..,"comments":..} / {"stars":..}
    content: str | None = None
    content_level: int = 0
    lang: str | None = None
    tags_hint: list[str] = Field(default_factory=list)      # source-provided topics/labels
    raw: dict[str, Any] = Field(default_factory=dict)


@dataclass
class FetchResult:
    source: str
    items: list[RawItem] = field(default_factory=list)
    calls: int = 0
    error: str | None = None
    partial_errors: list[str] = field(default_factory=list)   # e.g. one RSS feed failed
    ok_subsources: list[str] = field(default_factory=list)    # e.g. rss:<feed_id> that succeeded (even with 0 items)
    month_budget: int | None = None
    started_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished_at: datetime | None = None

    @property
    def ok(self) -> bool:
        return self.error is None


class RateLimiter:
    def __init__(self, min_interval_s: float):
        self.min_interval_s = min_interval_s
        self._last = 0.0

    def wait(self):
        now = time.monotonic()
        delta = now - self._last
        if delta < self.min_interval_s:
            time.sleep(self.min_interval_s - delta)
        self._last = time.monotonic()


class TransientHTTPError(Exception):
    pass


class BaseFetcher(ABC):
    name: str = "base"
    min_interval_s: float = 1.0
    month_budget: int | None = None      # max HTTP calls per month; None = unlimited
    timeout_s: float = 20.0
    user_agent: str = "techradar/0.1 (+personal tech radar)"

    def __init__(self, config: dict | None = None):
        self.config = config or {}
        self._limiter = RateLimiter(self.min_interval_s)
        self._calls = 0
        self._client: httpx.Client | None = None

    # ---- HTTP helpers -------------------------------------------------
    @property
    def client(self) -> httpx.Client:
        if self._client is None:
            self._client = httpx.Client(
                timeout=self.timeout_s,
                headers={"User-Agent": self.user_agent, **self.extra_headers()},
                follow_redirects=True,
            )
        return self._client

    def extra_headers(self) -> dict[str, str]:
        return {}

    @retry(
        reraise=True,
        stop=stop_after_attempt(4),
        wait=wait_exponential(multiplier=1, min=2, max=30),
        retry=retry_if_exception_type((TransientHTTPError, httpx.TransportError)),
    )
    def get(self, url: str, **kw) -> httpx.Response:
        self._limiter.wait()
        self._calls += 1
        r = self.client.get(url, **kw)
        retry_after = r.headers.get("Retry-After")
        transient = r.status_code in (429, 500, 502, 503, 504) or (
            r.status_code == 403 and (retry_after or "rate limit" in r.text[:300].lower())
        )
        if transient:
            if retry_after and retry_after.isdigit():
                time.sleep(min(int(retry_after), 60))
            raise TransientHTTPError(f"{r.status_code} {url}")
        r.raise_for_status()
        return r

    def get_json(self, url: str, **kw) -> Any:
        return self.get(url, **kw).json()

    # ---- contract -----------------------------------------------------
    @abstractmethod
    def fetch(self) -> Iterable[RawItem]:
        """Yield RawItems. May raise; `run()` catches."""

    def run(self, budget_check: Callable[[str, int], bool] | None = None) -> FetchResult:
        res = FetchResult(source=self.name)
        res.month_budget = self.month_budget
        try:
            if budget_check and not budget_check(self.name, self.month_budget or 0):
                res.error = "month budget exhausted"
                return res
            for it in self.fetch():          # append one by one so a mid-stream failure keeps earlier items
                res.items.append(it)
        except Exception as e:  # noqa: BLE001
            log.exception("fetcher %s failed", self.name)
            res.error = f"{type(e).__name__}: {e}"[:500]
        else:
            errs = self.config.pop("_errors", None) or []
            res.partial_errors.extend(errs)
            res.ok_subsources.extend(self.config.pop("_ok", None) or [])
            if errs and not res.items:
                res.error = "all sub-sources failed: " + "; ".join(errs)[:400]
        finally:
            res.calls = self._calls
            res.finished_at = datetime.now(timezone.utc)
            if self._client is not None:
                self._client.close()
                self._client = None
        return res


# ---- registry ------------------------------------------------------------
registry: dict[str, type[BaseFetcher]] = {}


def register(cls: type[BaseFetcher]) -> type[BaseFetcher]:
    registry[cls.name] = cls
    return cls


def get_fetcher(name: str, config: dict | None = None) -> BaseFetcher:
    # import side-effect registration
    from . import hn, github, rss  # noqa: F401
    if name not in registry:
        raise KeyError(f"unknown fetcher {name!r}; known: {sorted(registry)}")
    return registry[name](config)


def all_fetchers() -> list[str]:
    from . import hn, github, rss  # noqa: F401
    return sorted(registry)
