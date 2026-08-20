"""canonical_key rules — docs/02 §3."""
from __future__ import annotations

import hashlib
import logging
import re
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

log = logging.getLogger(__name__)

# exact tracking params + utm_* prefix (avoid over-matching refresh/source_lang/...)
TRACKING_PARAMS = {
    "ref", "ref_src", "ref_url", "source", "src", "from", "fbclid", "gclid", "igshid", "spm",
    "mc_cid", "mc_eid", "yclid", "_hsenc", "_hsmi", "mkt_tok", "s", "share_source", "vd_source",
}
GITHUB_NON_REPO = {
    "topics", "orgs", "features", "marketplace", "sponsors", "settings", "login", "about",
    "trending", "explore", "search", "collections", "events", "issues", "pulls", "notifications",
    "new", "codespaces", "security", "enterprise", "pricing", "team", "customer-stories",
    "readme", "site", "apps", "join", "signup", "discussions", "copilot", "blog", "contact",
}
GITHUB_RE = re.compile(r"^/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+?)(?:\.git)?(?:/(.*))?$")
ARXIV_NEW_RE = re.compile(r"(\d{4}\.\d{4,5})(?:v\d+)?")
ARXIV_OLD_RE = re.compile(r"([a-z-]+(?:\.[A-Z]{2})?/\d{7})(?:v\d+)?")
DOI_RE = re.compile(r"(10\.\d{4,9}/[^\s?#]+)", re.I)
HF_RE = re.compile(r"^/(?:(models|datasets|spaces)/)?([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)")
HF_NON_MODEL = {
    "papers", "blog", "docs", "spaces", "collections", "tasks", "learn", "pricing", "join",
    "login", "settings", "organizations", "enterprise", "chat", "posts", "search", "api",
    "huggingface", "front", "assets", "brand", "terms", "privacy", "support",
}
SHORT_HOSTS = {"bit.ly", "t.co", "goo.gl", "tinyurl.com", "buff.ly", "ow.ly", "is.gd", "lnkd.in",
               "dlvr.it", "ift.tt", "trib.al", "j.mp", "rb.gy", "cutt.ly", "shorturl.at", "s.id"}


def expand_short_url(url: str, timeout: float = 8.0) -> str:
    """Resolve known short links via HEAD; returns original url on any failure. Network call — used by ingest, not by canonical_key()."""
    try:
        host = urlsplit(url).netloc.lower()
    except Exception:
        return url
    if host.startswith("www."):
        host = host[4:]
    if host not in SHORT_HOSTS:
        return url
    try:
        import httpx
        with httpx.Client(timeout=timeout, follow_redirects=True) as c:
            r = c.head(url)
            return str(r.url) if r.url else url
    except Exception as e:  # noqa: BLE001
        log.debug("short url expand failed for %s: %s", url, e)
        return url


def normalize_url(url: str) -> str:
    url = url.strip()
    if not re.match(r"^https?://", url, re.I):
        url = "https://" + url
    parts = urlsplit(url)
    scheme = "https"
    netloc = parts.netloc.lower()
    if netloc.startswith("www."):
        netloc = netloc[4:]
    if netloc.endswith(":443") or netloc.endswith(":80"):
        netloc = netloc.rsplit(":", 1)[0]
    path = re.sub(r"/+$", "", parts.path) or "/"
    q = [(k, v) for k, v in parse_qsl(parts.query, keep_blank_values=False)
         if not (k.lower() in TRACKING_PARAMS or k.lower().startswith("utm_"))]
    q.sort()
    return urlunsplit((scheme, netloc, path, urlencode(q), ""))


def _url_hash_key(n: str) -> tuple[str, str]:
    return f"url:{hashlib.sha1(n.encode()).hexdigest()[:20]}", "article"


def canonical_key(url: str) -> tuple[str, str]:
    """Return (canonical_key, kind). Pure function, no network."""
    n = normalize_url(url)
    p = urlsplit(n)
    host, path = p.netloc, p.path

    if host == "github.com":
        m = GITHUB_RE.match(path)
        if m:
            owner, repo, rest = m.group(1).lower(), m.group(2).lower(), (m.group(3) or "")
            if owner not in GITHUB_NON_REPO:
                first = rest.split("/")[0] if rest else ""
                if first == "releases" and rest.count("/") >= 1:
                    return f"gh:{owner}/{repo}#{rest.split('/', 1)[1].strip('/')}", "release"
                if first in ("issues", "pull", "discussions") and rest.count("/") >= 1:
                    num = rest.split("/")[1]
                    return f"gh:{owner}/{repo}#{first}/{num}", "post"
                return f"gh:{owner}/{repo}", "repo"
        return _url_hash_key(n)
    if host in ("arxiv.org", "export.arxiv.org", "alphaxiv.org", "browse.arxiv.org"):
        m = ARXIV_NEW_RE.search(path)
        if m:
            return f"arxiv:{m.group(1)}", "paper"
        m = ARXIV_OLD_RE.search(path)
        if m:
            return f"arxiv:{m.group(1)}", "paper"
    if host in ("doi.org", "dx.doi.org"):
        m = DOI_RE.search(path)
        if m:
            return f"doi:{m.group(1).lower()}", "paper"
    if host == "huggingface.co":
        if path.startswith("/papers/"):
            m = ARXIV_NEW_RE.search(path)
            if m:
                return f"arxiv:{m.group(1)}", "paper"
        m = HF_RE.match(path)
        if m and m.group(2).lower() not in HF_NON_MODEL:
            kind = m.group(1) or "models"
            return f"hf:{kind}/{m.group(2).lower()}/{m.group(3).lower()}", "repo"
    if host == "news.ycombinator.com":
        q = dict(parse_qsl(p.query))
        if "id" in q:
            return f"hn:{q['id']}", "post"
    return _url_hash_key(n)
