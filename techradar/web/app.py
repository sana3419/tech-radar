"""Web: single unread stream + inbox + item detail. HTMX for actions. Bound to localhost by default."""
from __future__ import annotations

from datetime import date
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from ..db import session_scope
from ..services.feedback import list_inbox, record_feedback
from ..services.items import get_item, search_items, today_feed
from ..services.health import list_sources_health
from ..services.usage import usage as llm_usage

import logging

log = logging.getLogger(__name__)
app = FastAPI(title="TechRadar")


@app.middleware("http")
async def _token_gate(request: Request, call_next):
    from ..settings import get_settings
    tok = get_settings().web_token
    if tok and request.url.path != "/static/htmx.min.js":
        supplied = request.query_params.get("token") or request.cookies.get("tr_token")
        if supplied != tok:
            from fastapi.responses import PlainTextResponse
            return PlainTextResponse("403: 打开时在网址后加 ?token=<你的token>（之后免输）", status_code=403)
        resp = await call_next(request)
        if request.query_params.get("token") == tok:
            resp.set_cookie("tr_token", tok, max_age=180 * 24 * 3600, httponly=True, samesite="lax")
        return resp
    return await call_next(request)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")
templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
ACTIONS = ("save", "ignore", "read", "unsave", "dig", "click")


def _srcs(sources, extra=None) -> str:
    from ..digest.daily import SRC_ABBR, _feed_labels
    labels = _feed_labels()
    out = set()
    names = [s["source"] for s in sources] + list(extra or [])
    for src in names:
        out.add(labels.get(src) or SRC_ABBR.get(src.split(":")[0], src.split(":")[0]))
    return "+".join(sorted(out))


templates.env.filters["srcs"] = _srcs

_CHIP_CLS = {"hackernews": ("HN", "hn"), "github": ("GH", "gh")}


def _chips(sources, extra=None):
    from markupsafe import Markup, escape
    from ..digest.daily import SRC_ABBR, _feed_labels
    labels = _feed_labels()
    seen, out = set(), []
    names = [s["source"] for s in sources] + list(extra or [])
    for src in names:
        fam = src.split(":")[0]
        if fam in _CHIP_CLS:
            label, cls = _CHIP_CLS[fam]
        elif fam == "rss":
            label = labels.get(src) or src.split(":", 1)[-1]
            low = (src + label).lower()
            cls = "arxiv" if "arxiv" in low else ("zh" if any(k in low for k in ("juejin", "v2ex", "掘金")) else ("rel" if "release" in low or "gh_release" in low else "zh"))
        else:
            label, cls = SRC_ABBR.get(fam, fam), "zh"
        if label in seen:
            continue
        seen.add(label)
        out.append(f'<span class="chip {cls}">{escape(label)}</span>')
    return Markup("".join(out))


templates.env.filters["chips"] = _chips


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    from ..services.webview import home_digest, sidebar, unread_count
    with session_scope() as s:
        hd = home_digest(s)
        sb = sidebar(s)
        u = llm_usage(s)
        n_unread = unread_count(s)
    return templates.TemplateResponse(request, "index.html", {
        "hd": hd, "sb": sb, "usage": u, "n_unread": n_unread, "title": "今日"})


@app.get("/feed", response_class=HTMLResponse)
def feed(request: Request, offset: int = 0):
    """Infinite-scroll partial: unread items beyond the digest."""
    from ..services.webview import home_digest, unread_rest
    with session_scope() as s:
        hd = home_digest(s)
        exclude = {c["id"] for c in hd["cards"]}
        items = unread_rest(s, exclude, offset=offset, limit=20)
    return templates.TemplateResponse(request, "_feed.html", {"items": items, "offset": offset})


@app.get("/inbox", response_class=HTMLResponse)
def inbox(request: Request):
    with session_scope() as s:
        rows = list_inbox(s, limit=200)
    groups: dict[str, list] = {}
    for r in rows:
        key = (r.get("entities") or ["未分组"])[0] if r.get("entities") else "未分组"
        groups.setdefault(key, []).append(r)
    groups = dict(sorted(groups.items(), key=lambda kv: (kv[0] == "未分组", kv[0])))
    return templates.TemplateResponse(request, "inbox.html", {"groups": groups, "title": "收藏夹"})


@app.get("/search", response_class=HTMLResponse)
def search(request: Request, q: str = "", saved: int = 0, days: int = 0):
    rows, answer = [], None
    query = q.strip()
    if query:
        from datetime import datetime, timedelta, timezone
        since = datetime.now(timezone.utc) - timedelta(days=days) if days else None
        with session_scope() as s:
            if query.endswith(("?", "？")):
                from fastapi.responses import RedirectResponse
                from urllib.parse import quote
                return RedirectResponse(f"/ask?q={quote(query)}", status_code=303)
            rows = search_items(s, query, only_saved=bool(saved), since=since, limit=50)
    return templates.TemplateResponse(request, "search.html",
                                      {"rows": rows, "q": q, "saved": saved, "days": days, "title": "搜索"})


@app.get("/entity/{name}", response_class=HTMLResponse)
def entity(request: Request, name: str):
    from sqlalchemy import select
    from ..models import Entity
    from ..services.entities import entity_overview
    with session_scope() as s:
        e = s.scalar(select(Entity).where(Entity.canonical_name == name))
        if e is None:
            raise HTTPException(404, "未收录该实体；在 config/subscriptions.yaml 的 entities 里添加")
        ov = entity_overview(s, e, limit=60)
        # research reports for this entity (by filename match)
        from ..render.obsidian import vault_dir, _safe
        rp = []
        rdir = vault_dir() / "research"
        if rdir.exists():
            key = _safe(name).lower()
            rp = [p.stem for p in sorted(rdir.glob("*.md"), reverse=True) if key.split("-")[0] in p.stem.lower()][:10]
    # group timeline by date
    days: dict[str, list] = {}
    for t in ov["timeline"]:
        days.setdefault(t["ts"], []).append(t)
    return templates.TemplateResponse(request, "entity.html",
                                      {"ov": ov, "days": days, "reports": rp, "title": name})


@app.post("/entity/{name}/watch", response_class=HTMLResponse)
def entity_watch(request: Request, name: str):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    from sqlalchemy import select
    from ..models import Entity
    with session_scope() as s:
        e = s.scalar(select(Entity).where(Entity.canonical_name == name))
        if e is None:
            raise HTTPException(404)
        e.watched = not e.watched
        w = e.watched
    import html as _h
    from urllib.parse import quote
    return HTMLResponse(f'<button hx-post="/entity/{quote(_h.escape(name), safe="")}/watch" hx-swap="outerHTML">'
                        f'{"👀 已关注（点击取消）" if w else "关注变动"}</button>')


@app.get("/item/{item_id}", response_class=HTMLResponse)
def item(request: Request, item_id: int):
    with session_scope() as s:
        it = get_item(s, item_id)
    if not it:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "item.html", {"it": it, "title": it["title"][:40]})


@app.post("/act/{item_id}/{action}", response_class=HTMLResponse)
def act(request: Request, item_id: int, action: str):
    if action not in ACTIONS:
        raise HTTPException(400)
    # CSRF guard: only accept requests issued by htmx (custom header ⇒ CORS preflight blocks cross-site forms)
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403, "htmx only")
    try:
        with session_scope() as s:
            if action == "dig":
                from ..services.research import enqueue_research
                enqueue_research(s, item_id=item_id)
            r = record_feedback(s, item_id, action, channel="web")
    except KeyError:
        raise HTTPException(404)
    if action == "click":
        return HTMLResponse("")
    label = {"save": "已收藏 ⭐", "ignore": "已忽略", "read": "已读", "unsave": "已取消收藏", "dig": "已入队深挖 🔍"}[action]
    extra = f"（另隐藏 {r['hidden_similar']} 条相似）" if action == "ignore" and r.get("hidden_similar") else ""
    return HTMLResponse(f'<span class="done">{label}{extra}</span>')


@app.get("/config", response_class=HTMLResponse)
def config(request: Request):
    from ..services.tuning import muted, source_stats, topic_stats
    with session_scope() as s:
        topics = topic_stats(s)
        sources = source_stats(s)
        mutes = muted(s)
    return templates.TemplateResponse(request, "config.html",
                                      {"topics": topics, "sources": sources, "mutes": mutes, "title": "订阅调参"})


@app.post("/mute/{kind}/{key:path}", response_class=HTMLResponse)
def mute_route(request: Request, kind: str, key: str, days: int = 7):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    from ..services.feedback import mute as _mute
    with session_scope() as s:
        r = _mute(s, kind, key, days)
    return HTMLResponse(f'<span class="done">已静音至 {r["muted_until"][:10]}</span>')


@app.get("/ask", response_class=HTMLResponse)
def ask_page(request: Request, q: str = ""):
    from ..services.chatlog import recent_turns
    with session_scope() as s:
        turns = recent_turns(s, limit=12)
    return templates.TemplateResponse(request, "ask.html", {"turns": turns, "q": q, "title": "提问"})


@app.post("/ask", response_class=HTMLResponse)
async def ask_post(request: Request):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    form = await request.form()
    q = (form.get("q") or "").strip()
    web_mode = form.get("web") or "auto"        # auto | on | off
    web = {"on": True, "off": False}.get(web_mode)
    if not q:
        return HTMLResponse("")
    from ..agents.chat import ask
    from ..services.chatlog import recent_turns, record_turn
    with session_scope() as s:
        history = recent_turns(s, limit=3, within_minutes=45)   # same sitting only
        try:
            r = ask(s, q, history=history, web=web)
        except Exception as e:  # noqa: BLE001
            log.exception("ask failed")
            # a failed turn is not knowledge: don't persist it, don't let it pollute follow-up context
            turn = {"id": 0, "q": q, "a": f"回答失败：{type(e).__name__}，请重试", "citations": [],
                    "at": "", "cost": 0, "saved": False, "failed": True}
            return templates.TemplateResponse(request, "_turn.html", {"t": turn})
        t = record_turn(s, q, r)
        turn = {"id": t.id, "q": q, "a": r["answer"], "citations": r["citations"],
                "at": "", "cost": r.get("cost", 0), "saved": False,
                "web_count": r.get("web_count", 0)}
    return templates.TemplateResponse(request, "_turn.html", {"t": turn, "fresh": True})


@app.post("/ask/{task_id}/save", response_class=HTMLResponse)
def ask_save(request: Request, task_id: int):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    from ..render.notes import save_answer
    from ..services.chatlog import get_turn
    with session_scope() as s:
        t = get_turn(s, task_id)
        if not t:
            raise HTTPException(404)
        res = t.result or {}
        if not res.get("citations"):
            return HTMLResponse('<span class="done" style="color:var(--danger)">没有出处的回答不保存</span>')
        path = save_answer(s, (t.payload or {}).get("question", ""), res.get("answer", ""),
                           res.get("citations", []))
        t.result = {**res, "note_path": str(path)}
    return HTMLResponse(f'<span class="done">已存入 Obsidian · {path.name}</span>')


@app.post("/ingest", response_class=HTMLResponse)
async def ingest(request: Request):
    """Pull a web result found during Q&A into the radar (adds + saves it)."""
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    form = await request.form()
    url = (form.get("url") or "").strip()
    if not url.startswith("http"):
        raise HTTPException(400)
    from ..services.manual import add_url
    with session_scope() as s:
        r = add_url(s, url)
    return HTMLResponse(f'<span class="done">已收进雷达 #{r["id"]}</span>')


@app.post("/ask/clear", response_class=HTMLResponse)
def ask_clear(request: Request):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    from ..services.chatlog import clear_turns
    with session_scope() as s:
        n = clear_turns(s)
    return HTMLResponse(f'<div class="empty">已清空 {n} 条对话</div>')


@app.get("/research", response_class=HTMLResponse)
def research_list(request: Request):
    from sqlalchemy import select
    from ..models import AgentTask
    with session_scope() as s:
        tasks = list(s.scalars(select(AgentTask).where(AgentTask.type == "research")
                               .order_by(AgentTask.created_at.desc()).limit(50)).all())
        rows = []
        for t in tasks:
            st = (t.result or {}).get("structured") or {}
            rows.append({"id": t.id, "status": t.status, "created": t.created_at.isoformat()[:16],
                         "tldr": st.get("tldr") or (t.error or "")[:100],
                         "follow": (st.get("should_follow") or "")[:30],
                         "cost": float(t.cost_usd) if t.cost_usd else None,
                         "item_id": (t.payload or {}).get("item_id")})
    return templates.TemplateResponse(request, "research.html", {"rows": rows, "title": "深挖报告"})


@app.get("/research/{task_id}", response_class=HTMLResponse)
def research_detail(request: Request, task_id: int):
    from ..models import AgentTask
    with session_scope() as s:
        t = s.get(AgentTask, task_id)
        if not t or t.type != "research":
            raise HTTPException(404)
        report = (t.result or {}).get("report") or t.error or "（进行中）"
        item_id = (t.payload or {}).get("item_id")
    return templates.TemplateResponse(request, "research_detail.html",
                                      {"report": report, "t": t, "item_id": item_id, "title": f"深挖 #{task_id}"})


@app.post("/research", response_class=HTMLResponse)
async def research_new(request: Request):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    form = await request.form()
    target = (form.get("target") or "").strip()
    if not target:
        return HTMLResponse('<span class="done" style="color:crimson">请输入 URL 或 #条目id</span>')
    from ..services.research import enqueue_research
    from ..models import Item
    with session_scope() as s:
        if target.startswith("http"):
            from ..services.manual import add_url
            r = add_url(s, target)
            iid = r["id"]
        elif target.lstrip("#").isdigit():
            iid = int(target.lstrip("#"))
            if s.get(Item, iid) is None:
                return HTMLResponse(f'<span class="done" style="color:crimson">没有条目 #{iid}</span>')
        else:
            return HTMLResponse('<span class="done" style="color:crimson">请输入 URL 或 #条目id</span>')
        t = enqueue_research(s, item_id=iid)
    return HTMLResponse(f'<span class="done">已入队（任务 {t["task_id"]}），完成后 Telegram 推送并出现在列表</span>')


@app.get("/digests", response_class=HTMLResponse)
def digests_list(request: Request):
    from sqlalchemy import select
    from ..models import Digest
    with session_scope() as s:
        rows = [{"day": d.day.isoformat(), "kind": d.kind, "sent": bool(d.sent_at),
                 "stats": d.stats or {}} for d in
                s.scalars(select(Digest).order_by(Digest.day.desc()).limit(60)).all()]
    return templates.TemplateResponse(request, "digests.html", {"rows": rows, "title": "日报归档"})


@app.get("/digests/{day}", response_class=HTMLResponse)
def digest_day(request: Request, day: str, kind: str = "daily"):
    from datetime import date as _date
    from sqlalchemy import select
    from ..models import Digest
    try:
        dd = _date.fromisoformat(day)
    except ValueError:
        raise HTTPException(404)
    with session_scope() as s:
        d = s.scalar(select(Digest).where(Digest.day == dd, Digest.kind == kind))
        if not d:
            raise HTTPException(404)
        md = d.markdown or ""
    return templates.TemplateResponse(request, "digest_day.html", {"md": md, "day": day, "kind": kind, "title": f"日报 {day}"})


@app.post("/note/{item_id}", response_class=HTMLResponse)
async def note(request: Request, item_id: int):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    form = await request.form()
    text = (form.get("text") or "").strip()
    from ..models import Feedback
    from sqlalchemy import select
    with session_scope() as s:
        fb = s.scalar(select(Feedback).where(Feedback.item_id == item_id, Feedback.action == "save")
                      .order_by(Feedback.ts.desc()))
        if not fb:
            raise HTTPException(404)
        fb.note = text[:300] or None
    return HTMLResponse('<span class="done">已保存备注</span>')


@app.post("/act/{item_id}/undo", response_class=HTMLResponse)
def act_undo(request: Request, item_id: int):
    if request.headers.get("HX-Request") != "true":
        raise HTTPException(403)
    from sqlalchemy import select
    from ..models import Feedback, Item
    with session_scope() as s:
        fb = s.scalar(select(Feedback).where(Feedback.item_id == item_id,
                                             Feedback.action.in_(("ignore", "read")))
                      .order_by(Feedback.ts.desc()))
        if fb:
            s.delete(fb)
        it = s.get(Item, item_id)
        if it and it.status == "expired":
            it.status = "scored"
    return HTMLResponse('<span class="done">已撤销</span>')


@app.get("/health", response_class=HTMLResponse)
def health(request: Request):
    from datetime import date, timedelta
    from sqlalchemy import select
    from ..models import LlmUsage
    with session_scope() as s:
        rows = list_sources_health(s)
        usage7 = [{"day": u.day.isoformat(), "calls": u.calls, "cost": float(u.cost_usd or 0)}
                  for u in s.scalars(select(LlmUsage).where(LlmUsage.day >= date.today() - timedelta(days=7))
                                     .order_by(LlmUsage.day.desc())).all()]
    return templates.TemplateResponse(request, "health.html", {"rows": rows, "usage7": usage7, "title": "系统"})


def run(host: str = "127.0.0.1", port: int = 8765):
    import uvicorn
    uvicorn.run(app, host=host, port=port)
