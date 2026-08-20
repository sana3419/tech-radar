"""Fetcher contract tests (no network). xfail = known gap in current impl."""
from datetime import timezone

import httpx
import pytest

from techradar.fetchers.base import BaseFetcher, RawItem, TransientHTTPError
from techradar.fetchers.hn import HackerNewsFetcher
from techradar.fetchers.rss import RSSFetcher

ATOM = b"""<?xml version="1.0"?><feed xmlns="http://www.w3.org/2005/Atom"><title>t</title>
<entry><id>e1</id><title>Hello</title><link href="https://example.com/p?utm_source=x"/>
<published>2026-08-17T23:58:14+00:00</published><author><name>A</name></author></entry></feed>"""


class _Resp:
    def __init__(self, content=b"", status=200, json=None):
        self.content, self.status_code, self._json = content, status, json

    def json(self):
        return self._json


def test_run_swallows_fetch_exceptions_and_records_error():
    class Boom(BaseFetcher):
        name = "boom"

        def fetch(self):
            raise RuntimeError("kaboom")

    res = Boom().run()
    assert res.error and "kaboom" in res.error and res.items == [] and res.finished_at is not None


def test_run_swallows_generator_exceptions():
    class Boom(BaseFetcher):
        name = "boom2"

        def fetch(self):
            yield RawItem(source="boom2", external_id="1", title="a", url="https://a.example")
            raise ValueError("mid-stream")

    res = Boom().run()
    assert res.error and "mid-stream" in res.error



def test_run_keeps_partial_results_on_midstream_failure():
    class Boom(BaseFetcher):
        name = "boom3"

        def fetch(self):
            yield RawItem(source="boom3", external_id="1", title="a", url="https://a.example")
            raise ValueError("page 2 failed")

    res = Boom().run()
    assert len(res.items) == 1 and res.error


def test_budget_check_blocks_run():
    class F(BaseFetcher):
        name = "f"
        month_budget = 10

        def fetch(self):
            yield RawItem(source="f", external_id="1", title="a", url="https://a.example")

    res = F().run(budget_check=lambda name, budget: False)
    assert res.error == "month budget exhausted" and res.items == []


def test_get_retries_on_transient_then_succeeds(monkeypatch):
    class F(BaseFetcher):
        name = "f2"
        min_interval_s = 0

        def fetch(self):
            return []

    f = F()
    calls = {"n": 0}

    class C:
        def get(self, url, **kw):
            calls["n"] += 1
            if calls["n"] < 3:
                return httpx.Response(503, request=httpx.Request("GET", url))
            return httpx.Response(200, request=httpx.Request("GET", url), text="ok")

    monkeypatch.setattr(F, "client", property(lambda self: C()))
    monkeypatch.setattr("techradar.fetchers.base.time.sleep", lambda s: None)
    # neutralise tenacity waits
    f.get.retry.wait = lambda *a, **k: 0  # type: ignore[attr-defined]
    r = f.get("https://x")
    assert r.status_code == 200 and calls["n"] == 3 and f._calls == 3


def test_rss_one_bad_feed_does_not_kill_others(monkeypatch):
    f = RSSFetcher({"feeds": [{"id": "bad", "url": "https://bad"}, {"id": "good", "url": "https://good"}]})

    def fake_get(url, **kw):
        if "bad" in url:
            raise TransientHTTPError("503 bad")
        return _Resp(ATOM)

    monkeypatch.setattr(f, "get", fake_get)
    res = f.run()
    assert res.ok and len(res.items) == 1 and res.items[0].source == "rss:good"



def test_rss_feed_failure_is_visible_in_result(monkeypatch):
    f = RSSFetcher({"feeds": [{"id": "bad", "url": "https://bad"}]})
    monkeypatch.setattr(f, "get", lambda url, **kw: (_ for _ in ()).throw(TransientHTTPError("401")))
    res = f.run()
    assert res.error or getattr(res, "partial_errors", None), "feed failure should surface in FetchResult / source_health(rss:bad)"



def test_rss_published_at_is_utc(monkeypatch):
    f = RSSFetcher({"feeds": [{"id": "g", "url": "https://good"}]})
    monkeypatch.setattr(f, "get", lambda url, **kw: _Resp(ATOM))
    it = list(f.fetch())[0]
    assert it.published_at.tzinfo is not None
    assert it.published_at.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S") == "2026-08-17T23:58:14"


def test_hn_maps_fields(monkeypatch):
    f = HackerNewsFetcher({"pages": 1})
    hits = [
        {"objectID": "1", "title": "Ask HN: x", "url": None, "author": "Bob", "created_at_i": 1700000000, "points": 40, "num_comments": 3, "story_text": "hi"},
        {"objectID": "2", "title": "Repo", "url": "https://github.com/o/r", "author": "A", "created_at_i": 1700000000, "points": 99, "num_comments": 0},
        {"objectID": "3", "title": "", "url": "https://x", "author": "A", "created_at_i": 1700000000},
    ]
    monkeypatch.setattr(f, "get_json", lambda url, **kw: {"hits": hits})
    items = list(f.fetch())
    assert [i.external_id for i in items] == ["1", "2"]
    a, b = items
    assert a.url == "https://news.ycombinator.com/item?id=1" and a.kind == "post" and a.content_level == 1
    assert b.url == "https://github.com/o/r" and b.source_url == "https://news.ycombinator.com/item?id=2"
    assert b.published_at.tzinfo is not None and b.metrics == {"points": 99, "comments": 0}
    assert a.author_key == "bob"
