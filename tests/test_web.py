"""Web UI acceptance: routes, escaping, action endpoints. All DB writes go through the rolled-back
test session (web.app.session_scope is monkeypatched), so nothing persists."""
from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timedelta, timezone

import pytest

from techradar.models import Feedback, Item, ItemSource
from tests.test_ingest import session  # noqa: F401

NOW = datetime.now(timezone.utc)
UNIQ = f"webtest-{int(NOW.timestamp())}"


@pytest.fixture
def client(session, monkeypatch):  # noqa: F811
    from fastapi.testclient import TestClient
    import techradar.web.app as webapp

    @contextmanager
    def _scope():
        yield session
        session.flush()

    monkeypatch.setattr(webapp, "session_scope", _scope)
    from techradar.settings import get_settings
    monkeypatch.setattr(get_settings(), "web_token", None)
    return TestClient(webapp.app, raise_server_exceptions=False, headers={"HX-Request": "true"})


def _mk(session, title, status="scored", score=99.0, **kw):  # noqa: F811
    it = Item(title=title, url=f"https://example.invalid/{UNIQ}/{abs(hash(title))}", canonical_key=f"{UNIQ}:{title}",
              kind="article", status=status, score=score, first_seen_at=NOW, last_seen_at=NOW, **kw)
    it.sources = [ItemSource(source="hackernews", external_id=f"{UNIQ}-{title}", metrics_raw={}, seen_at=NOW, raw={})]
    session.add(it)
    session.flush()
    return it


def test_index_and_health(client):
    r = client.get("/")
    assert r.status_code == 200 and "今日必读" in r.text
    assert client.get("/health").status_code == 200
    assert client.get("/inbox").status_code == 200


def test_index_shows_unread_and_hides_acted(client, session):  # noqa: F811
    """Beyond the digest, unread items live in the htmx-loaded /feed partial (index only ships the
    digest + a hx-get placeholder), so assert against /feed."""
    a = _mk(session, f"{UNIQ} unread alpha")
    b = _mk(session, f"{UNIQ} read beta")
    session.add(Feedback(item_id=b.id, action="read", channel="web"))
    session.flush()
    home = client.get("/").text
    assert 'hx-get="/feed?offset=0"' in home          # stream placeholder is wired up
    assert f'id="item-{b.id}"' not in home
    feed = client.get("/feed", params={"offset": 0}).text
    assert f'id="item-{a.id}"' in feed
    assert f'id="item-{b.id}"' not in feed


def test_search_escapes_query_and_titles(client, session):  # noqa: F811
    _mk(session, f"{UNIQ} <script>alert(1)</script> zzqx")
    r = client.get("/search", params={"q": "<script>alert(1)</script>"})
    assert r.status_code == 200
    assert "<script>alert(1)</script>" not in r.text
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in r.text
    # chinese query + saved flag
    assert client.get("/search", params={"q": "显存 推理", "saved": 1}).status_code == 200
    assert client.get("/search").status_code == 200


def test_item_detail_and_404(client, session):  # noqa: F811
    it = _mk(session, f"{UNIQ} detail <b>x</b>", summary_points=["p1 <i>", "p2"])
    r = client.get(f"/item/{it.id}")
    assert r.status_code == 200
    assert "&lt;b&gt;x&lt;/b&gt;" in r.text and "p1 &lt;i&gt;" in r.text
    assert client.get("/item/999999999").status_code == 404


def test_act_endpoints(client, session):  # noqa: F811
    it = _mk(session, f"{UNIQ} act gamma")
    for action, label in (("read", "已读"), ("save", "已收藏"), ("unsave", "已取消收藏")):
        r = client.post(f"/act/{it.id}/{action}")
        assert r.status_code == 200 and label in r.text
    acts = {f.action for f in session.query(Feedback).filter_by(item_id=it.id).all()}
    assert acts == {"read", "save", "unsave"}
    assert client.post(f"/act/{it.id}/bogus").status_code == 400



def test_act_unknown_item_is_404(client):
    assert client.post("/act/999999999/read").status_code == 404



def test_act_click_accepted(client, session):  # noqa: F811
    it = _mk(session, f"{UNIQ} click delta")
    assert client.post(f"/act/{it.id}/click").status_code == 200


def test_srcs_filter_matches_digest():
    from techradar.web.app import _srcs
    from techradar.digest.daily import _feed_labels, SRC_ABBR
    labels = _feed_labels()
    assert _srcs([{"source": "hackernews"}, {"source": "github"}]) == "GH+HN"
    for k, v in list(labels.items())[:3]:
        assert _srcs([{"source": k}]) == v
    assert _srcs([{"source": "rss:__nope__"}]) == SRC_ABBR["rss"]


def test_act_requires_htmx_header(client):
    from fastapi.testclient import TestClient
    from techradar.web import app as webapp
    plain = TestClient(webapp.app, raise_server_exceptions=False)
    assert plain.post("/act/1/read").status_code == 403   # cross-site form POST is rejected


def test_v2_write_routes_require_htmx_header():
    from fastapi.testclient import TestClient
    from techradar.web import app as webapp
    plain = TestClient(webapp.app, raise_server_exceptions=False)
    for path in ("/research", "/note/1", "/entity/x/watch", "/mute/source/x"):
        assert plain.post(path).status_code == 403, path


def test_v2_error_branches(client):
    assert client.get("/entity/__nope__").status_code == 404
    assert client.get("/research/999999999").status_code == 404
    assert client.get("/digests/2001-01-01").status_code == 404
    r = client.post("/research")                                  # empty target: friendly inline message
    assert r.status_code == 200 and "请输入" in r.text
    assert client.post("/entity/__nope__/watch").status_code == 404


def test_home_digest_numbers_match_persisted_positions(client, session):  # noqa: F811
    from techradar.digest.daily import DigestData, local_today, persist_digest
    a = _mk(session, f"{UNIQ} pos one")
    b = _mk(session, f"{UNIQ} pos two")
    c = _mk(session, f"{UNIQ} pos three")
    d = DigestData(day=local_today())
    d.top, d.folded = [a, b], [c]
    persist_digest(session, d, "md", sent=True)
    session.flush()
    html = client.get("/").text
    for n, it in ((1, a), (2, b), (3, c)):
        block = html.split(f'id="item-{it.id}"')[1][:300]
        assert f'class="no">{n}</span>' in block, (n, it.id)
