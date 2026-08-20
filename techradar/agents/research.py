"""Research assistant (🔍 深挖): gather evidence deterministically (page text / GitHub README+meta /
arXiv abstract / related local items), one structured LLM call, ≤500-char report → Telegram + reports/ (+ Obsidian dir if set)."""
from __future__ import annotations

import html
import json
import logging
import re
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..llm.client import model_research, structured
from ..llm.schemas import ResearchOut
from ..models import AgentTask, Entity, Item
from ..services.items import search_items
from ..settings import ROOT, get_settings

log = logging.getLogger(__name__)
UA = {"User-Agent": "Mozilla/5.0 techradar/0.1"}
MAX_CTX = 9000

SYSTEM = """你是用户的技术研究助手，要为一位后端/AI 方向开发者写一份“要不要跟进”的简短研判。只依据给定材料，不编造。
输出要求（中文，专有名词保留原文）：
- tldr：≤80 字，这是什么 + 核心价值。
- should_follow：只能是“是”“否”“观望”之一 + 一句理由（≤40 字）。
- key_facts：3-5 条硬事实（数字、能力、限制、成熟度、许可证、活跃度），每条 ≤40 字。
- relation_to_known：与“本地相关条目”的关系（引用 #id），没有就空。
- risks：1-3 条风险/疑点（不成熟、夸大、替代方案更好等）。
- sources：用到的 URL。"""


def _strip_html(t: str) -> str:
    t = re.sub(r"<(script|style|nav|footer|header)[^>]*>.*?</\1>", " ", t, flags=re.S | re.I)
    t = re.sub(r"<[^>]+>", " ", t)
    t = html.unescape(t)
    return re.sub(r"\s+", " ", t).strip()


def _fetch_text(url: str, limit: int = 6000) -> str:
    try:
        r = httpx.get(url, headers=UA, timeout=20, follow_redirects=True)
        ct = r.headers.get("content-type", "")
        if "html" in ct or "xml" in ct or "text" in ct:
            return _strip_html(r.text)[:limit]
        return ""
    except Exception as e:  # noqa: BLE001
        log.info("fetch failed %s: %s", url, e)
        return ""


def _github(owner_repo: str) -> dict:
    out = {}
    hdr = {**UA, "Accept": "application/vnd.github+json"}
    tok = get_settings().github_token
    if tok:
        hdr["Authorization"] = f"Bearer {tok}"
    try:
        m = httpx.get(f"https://api.github.com/repos/{owner_repo}", headers=hdr, timeout=20).json()
        out["meta"] = {k: m.get(k) for k in ("full_name", "description", "stargazers_count", "forks_count", "open_issues_count",
                                              "language", "license", "created_at", "pushed_at", "archived", "topics")}
        if out["meta"].get("license"):
            out["meta"]["license"] = (m.get("license") or {}).get("spdx_id")
        rd = httpx.get(f"https://api.github.com/repos/{owner_repo}/readme", headers={**hdr, "Accept": "application/vnd.github.raw"}, timeout=20)
        if rd.status_code == 200:
            out["readme"] = rd.text[:6000]
        rel = httpx.get(f"https://api.github.com/repos/{owner_repo}/releases?per_page=3", headers=hdr, timeout=20)
        if rel.status_code == 200:
            out["releases"] = [{"tag": r.get("tag_name"), "date": r.get("published_at")} for r in rel.json()[:3]]
    except Exception as e:  # noqa: BLE001
        log.info("github fetch failed: %s", e)
    return out


def _arxiv(aid: str) -> str:
    try:
        r = httpx.get(f"https://export.arxiv.org/api/query?id_list={aid}", headers=UA, timeout=20)
        m = re.search(r"<summary>(.*?)</summary>", r.text, re.S)
        return re.sub(r"\s+", " ", m.group(1)).strip()[:3000] if m else ""
    except Exception:  # noqa: BLE001
        return ""


def gather(session: Session, item: Item) -> tuple[str, list[str]]:
    parts, urls = [], [item.url]
    parts.append(f"条目 #{item.id}: {item.title}\nURL: {item.url}\n摘要: {item.summary_one or ''}\n要点: {item.summary_points or []}")
    key = item.canonical_key
    if key.startswith("gh:"):
        repo = key[3:].split("#")[0]
        g = _github(repo)
        if g:
            parts.append("GitHub 元数据: " + json.dumps(g.get("meta"), ensure_ascii=False))
            if g.get("releases"):
                parts.append("最近 releases: " + json.dumps(g["releases"], ensure_ascii=False))
            if g.get("readme"):
                parts.append("README（截断）:\n" + g["readme"])
            urls.append(f"https://github.com/{repo}")
    elif key.startswith("arxiv:"):
        ab = _arxiv(key[6:])
        if ab:
            parts.append("arXiv 摘要: " + ab)
    else:
        txt = _fetch_text(item.url)
        if txt:
            parts.append("页面正文（截断）:\n" + txt)
    if item.content and "页面正文" not in " ".join(parts):
        parts.append("已存正文（截断）:\n" + item.content[:3000])
    # related local items
    terms = " ".join(re.findall(r"[A-Za-z][A-Za-z0-9.+-]{2,}", item.title)[:4]) or item.title[:30]
    rel = [r for r in search_items(session, terms, limit=8) if r["id"] != item.id][:5]
    if rel:
        parts.append("本地相关条目:\n" + "\n".join(f"#{r['id']} {r['title'][:80]} — {r.get('summary_one') or ''} ({r['url']})" for r in rel))
    ctx = "\n\n".join(parts)
    if len(ctx) > MAX_CTX:
        ctx = ctx[:MAX_CTX] + "\n…（截断）"
    return ctx, urls


def render_report(item: Item, out: ResearchOut, question: str | None) -> str:
    lines = [f"🔍 深挖：{item.title[:70]}", item.url, ""]
    if question:
        lines.append(f"问题：{question}")
    lines.append(f"TL;DR：{out.tldr}")
    lines.append(f"跟进建议：{out.should_follow}")
    if out.key_facts:
        lines.append("关键事实：")
        lines += [f"• {f}" for f in out.key_facts[:5]]
    if out.relation_to_known:
        lines.append("与已知的关系：" + "；".join(out.relation_to_known[:3]))
    if out.risks:
        lines.append("风险：" + "；".join(out.risks[:3]))
    return "\n".join(lines)


def _write_markdown(item: Item, out: ResearchOut, report: str) -> Path:
    from ..render.obsidian import _safe
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-zA-Z0-9一-鿿]+", "-", item.title[:50]).strip("-") or f"item-{item.id}"
    ents = item.entities_matched or []
    fm = [f"item_id: {item.id}", f"date: {day}", "type: research"]
    if ents:
        fm.append("entities: [" + ", ".join(ents) + "]")     # keep early: backlink scan reads the head
    fm += [f"should_follow: {out.should_follow[:60]}", f"url: {item.url}"]
    backlinks = ("\n\n## 相关实体\n\n" + " · ".join(f"[[entities/{_safe(e)}|{e}]]" for e in ents)) if ents else ""
    md = ("---\n" + "\n".join(fm) + "\n---\n\n"
          f"# {item.title}\n\n{report}\n\n## 来源\n" + "\n".join(f"- {u}" for u in out.sources)
          + backlinks + "\n")
    from ..render.obsidian import vault_dir
    targets = [ROOT / "reports", vault_dir() / "research"]
    last = None
    for t in targets:
        try:
            t.mkdir(parents=True, exist_ok=True)
            last = t / f"{day}-{slug}.md"
            last.write_text(md, encoding="utf-8")
        except Exception as e:  # noqa: BLE001
            log.warning("write report failed %s: %s", t, e)
    return last


def run_task(session: Session, task: AgentTask) -> dict:
    payload = task.payload or {}
    item = None
    if payload.get("item_id"):
        item = session.scalar(select(Item).options(selectinload(Item.sources)).where(Item.id == payload["item_id"]))
    elif payload.get("entity_id"):
        ent = session.get(Entity, payload["entity_id"])
        if ent:
            rows = search_items(session, ent.canonical_name, limit=1)
            if rows:
                item = session.get(Item, rows[0]["id"])
    if item is None:
        raise KeyError("item not found for task")
    question = payload.get("question")
    ctx, urls = gather(session, item)
    user = (f"用户追问：{question}\n\n" if question else "") + "材料：\n" + ctx
    out, meta = structured(session, ResearchOut, system=SYSTEM, user=user, model=model_research(), max_tokens=2500)
    if not out.sources:
        out.sources = urls
    report = render_report(item, out, question)
    path = _write_markdown(item, out, report)
    # mark entities watched
    for name in item.entities_matched or []:
        e = session.scalar(select(Entity).where(Entity.canonical_name == name))
        if e:
            e.watched = True
    return {"report": report, "structured": out.model_dump(), "meta": meta, "path": str(path) if path else None}


def run_pending(session: Session, max_tasks: int = 2) -> int:
    from sqlalchemy import text
    tasks = session.scalars(
        select(AgentTask).where(AgentTask.type == "research", AgentTask.status == "pending")
        .order_by(AgentTask.created_at).limit(max_tasks).with_for_update(skip_locked=True)
    ).all()
    n = 0
    for t in tasks:
        t.status, t.attempts = "running", (t.attempts or 0) + 1
        session.commit()
        try:
            res = run_task(session, t)
            t.result = {"report": res["report"], "structured": res["structured"], "path": res["path"]}
            t.model = res["meta"]["model"]; t.tokens_in = res["meta"]["tokens_in"]; t.tokens_out = res["meta"]["tokens_out"]
            t.cost_usd = res["meta"]["cost"]; t.status = "done"; t.finished_at = datetime.now(timezone.utc)
            session.commit()
            _notify(res["report"] + f"\n\n💰 ${res['meta']['cost']:.4f}")
            n += 1
        except Exception as e:  # noqa: BLE001
            log.exception("research task %s failed", t.id)
            t.error = str(e)[:500]
            t.status = "failed" if (t.attempts or 0) >= 2 else "pending"
            session.commit()
            if t.status == "failed":
                _notify(f"🔍 深挖任务 {t.id} 失败：{type(e).__name__}: {str(e)[:120]}")
    return n


def _notify(text: str) -> None:
    try:
        from ..bot.telegram import send_text
        if get_settings().telegram_bot_token:
            send_text(text)
    except Exception as e:  # noqa: BLE001
        log.warning("notify failed: %s", e)
