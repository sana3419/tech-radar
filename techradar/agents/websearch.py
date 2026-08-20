"""Live web lookup for the Q&A agent.

Local memory only knows what the fetchers already collected. When a question reaches past that,
this searches the *live* web and reads the pages, so the answer can cover things never ingested.

Providers, in order of preference:
  1. Vertical tech APIs — Hacker News (Algolia), GitHub, arXiv. No key, stable, and they are the
     primary sources for this domain anyway.
  2. A general web-search provider (Tavily / Brave / Serper) when an API key is configured.
Pages are then fetched and reduced to text.

SECURITY: fetched page text is untrusted input. It is passed to the model as *data* only, clearly
fenced and labelled; the prompt tells the model to ignore any instructions found inside it.
"""
from __future__ import annotations

import concurrent.futures as cf
import html as _html
import logging
import re
from dataclasses import dataclass, field

import httpx

from ..settings import get_settings

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) techradar/0.1"}
TIMEOUT = 15
PAGE_CHARS = 4000


@dataclass
class WebHit:
    title: str
    url: str
    snippet: str = ""
    source: str = "web"
    text: str = ""                       # filled in by fetch_pages
    metrics: dict = field(default_factory=dict)


# ---------------------------------------------------------------- providers
def _hn(query: str, limit: int) -> list[WebHit]:
    r = httpx.get("https://hn.algolia.com/api/v1/search",
                  params={"query": query, "tags": "story", "hitsPerPage": limit},
                  headers=UA, timeout=TIMEOUT)
    r.raise_for_status()
    out = []
    for h in r.json().get("hits", []):
        url = h.get("url") or f"https://news.ycombinator.com/item?id={h['objectID']}"
        out.append(WebHit(title=(h.get("title") or "").strip(), url=url, source="hackernews",
                          snippet=(h.get("story_text") or "")[:300],
                          metrics={"points": h.get("points"), "comments": h.get("num_comments")}))
    return out


def _github(query: str, limit: int) -> list[WebHit]:
    hdr = dict(UA, Accept="application/vnd.github+json")
    if get_settings().github_token:
        hdr["Authorization"] = f"Bearer {get_settings().github_token}"
    r = httpx.get("https://api.github.com/search/repositories",
                  params={"q": query, "sort": "stars", "per_page": limit}, headers=hdr, timeout=TIMEOUT)
    r.raise_for_status()
    return [WebHit(title=f"{i['full_name']}: {i.get('description') or ''}".strip(": "),
                   url=i["html_url"], source="github", snippet=(i.get("description") or "")[:300],
                   metrics={"stars": i.get("stargazers_count")})
            for i in r.json().get("items", [])]


def _arxiv(query: str, limit: int) -> list[WebHit]:
    r = httpx.get("https://export.arxiv.org/api/query",
                  params={"search_query": f"all:{query}", "max_results": limit,
                          "sortBy": "relevance", "sortOrder": "descending"},
                  headers=UA, timeout=20)
    r.raise_for_status()
    out = []
    for entry in re.findall(r"<entry>(.*?)</entry>", r.text, re.S):
        t = re.search(r"<title>(.*?)</title>", entry, re.S)
        link = re.search(r'<id>(.*?)</id>', entry, re.S)
        summ = re.search(r"<summary>(.*?)</summary>", entry, re.S)
        if t and link:
            url = link.group(1).strip().replace("http://", "https://")   # http endpoint fails TLS
            out.append(WebHit(title=re.sub(r"\s+", " ", _html.unescape(t.group(1))).strip(),
                              url=url, source="arxiv",
                              snippet=re.sub(r"\s+", " ", _html.unescape(summ.group(1))).strip()[:400] if summ else ""))
    return out


def _brave(query: str, limit: int) -> list[WebHit]:
    key = get_settings().brave_api_key
    if not key:
        return []
    r = httpx.get("https://api.search.brave.com/res/v1/web/search",
                  params={"q": query, "count": min(limit, 20), "text_decorations": 0,
                          "result_filter": "web"},
                  headers={**UA, "Accept": "application/json", "X-Subscription-Token": key},
                  timeout=TIMEOUT)
    r.raise_for_status()
    results = (r.json().get("web") or {}).get("results") or []
    return [WebHit(title=_strip(i.get("title", ""))[:200], url=i.get("url", ""), source="web",
                   snippet=_strip(i.get("description") or "")[:400],
                   metrics={"age": i.get("age")} if i.get("age") else {})
            for i in results if i.get("url")]


def _tavily(query: str, limit: int) -> list[WebHit]:
    key = get_settings().tavily_api_key
    if not key:
        return []
    r = httpx.post("https://api.tavily.com/search",
                   json={"api_key": key, "query": query, "max_results": limit,
                         "search_depth": "basic", "include_answer": False},
                   timeout=TIMEOUT)
    r.raise_for_status()
    return [WebHit(title=i.get("title", "")[:200], url=i.get("url", ""), source="web",
                   snippet=(i.get("content") or "")[:400]) for i in r.json().get("results", [])]


def _general(query: str, limit: int) -> list[WebHit]:
    """General web search: Brave first, Tavily as fallback; empty when neither key is set."""
    for fn in (_brave, _tavily):
        try:
            hits = fn(query, limit)
        except Exception as e:  # noqa: BLE001
            log.info("general search %s failed: %s", fn.__name__, e)
            continue
        if hits:
            return hits
    return []


PROVIDERS = {"hackernews": _hn, "github": _github, "arxiv": _arxiv, "web": _general}


# ---------------------------------------------------------------- search + read
CJK_RE = re.compile(r"[\u4e00-\u9fff]+")
_CJK_STOP = ("的", "了", "吗", "呢", "怎么样", "支持得", "情况", "现在", "有什么", "是什么", "怎么",
             "如何", "哪些", "介绍", "一下", "这个", "那个", "可以", "能否", "怎样")


def clean_query(query: str, keep_cjk: bool = True) -> str:
    """Search APIs match literally: Chinese particles glued onto a query kill recall.

    Keeps ASCII tech terms as-is and strips filler from the Chinese part; if the query is pure
    Chinese the cleaned Chinese is kept (some sources do index Chinese).
    """
    q = query.strip()
    for w in _CJK_STOP:
        q = q.replace(w, " ")
    ascii_terms = re.findall(r"[A-Za-z][A-Za-z0-9.+#_-]{1,}", q)
    cjk_terms = [t for t in CJK_RE.findall(q) if len(t) >= 2]
    terms = ascii_terms + (cjk_terms if (keep_cjk and not ascii_terms) else [])
    return " ".join(dict.fromkeys(terms))[:120] or query[:120]


def search(query: str, limit_per_source: int = 3, sources: list[str] | None = None) -> list[WebHit]:
    """Query providers in parallel; one failing provider never fails the whole search."""
    names = sources or list(PROVIDERS)
    # ASCII-only providers get the cleaned query; keep the original for Chinese-capable ones
    query = clean_query(query)
    hits: list[WebHit] = []
    with cf.ThreadPoolExecutor(max_workers=len(names)) as pool:
        futs = {pool.submit(PROVIDERS[n], query, limit_per_source): n for n in names if n in PROVIDERS}
        for f in cf.as_completed(futs, timeout=40):
            try:
                hits.extend(f.result() or [])
            except Exception as e:  # noqa: BLE001
                log.info("web search provider %s failed: %s", futs[f], e)
    seen, by_source = set(), {}
    for h in hits:
        k = re.sub(r"[#?].*$", "", h.url.rstrip("/")).lower()
        if k and k not in seen and h.title:
            seen.add(k)
            by_source.setdefault(h.source, []).append(h)
    # Interleave by source. Concatenating completion order let a slow provider's hits be cut off by
    # the caller's top-N slice; round-robin guarantees every source is represented.
    order = [s for s in ("web", "github", "hackernews", "arxiv") if s in by_source]
    order += [s for s in by_source if s not in order]
    out = []
    for i in range(max((len(v) for v in by_source.values()), default=0)):
        for src in order:
            if i < len(by_source[src]):
                out.append(by_source[src][i])
    return out


def _strip(t: str) -> str:
    t = re.sub(r"<(script|style|nav|footer|header|svg)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    return re.sub(r"\s+", " ", _html.unescape(t)).strip()


def _read_one(h: WebHit) -> WebHit:
    try:
        r = httpx.get(h.url, headers=UA, timeout=TIMEOUT, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and any(x in ct for x in ("html", "text", "xml", "json")):
            h.text = _strip(r.text)[:PAGE_CHARS]
    except Exception as e:  # noqa: BLE001
        log.info("page read failed %s: %s", h.url, e)
    return h


def fetch_pages(hits: list[WebHit], top: int = 4) -> list[WebHit]:
    """Read the body of the most promising hits (the rest keep their snippet)."""
    targets = hits[:top]
    with cf.ThreadPoolExecutor(max_workers=min(4, len(targets) or 1)) as pool:
        list(pool.map(_read_one, targets))
    return hits


def search_and_read(query: str, limit_per_source: int = 3, read_top: int = 4,
                    sources: list[str] | None = None) -> list[WebHit]:
    return fetch_pages(search(query, limit_per_source, sources), top=read_top)
