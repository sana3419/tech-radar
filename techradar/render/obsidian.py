"""Obsidian projection (read-only generated pages) — docs/02 §7.

<vault>/TechRadar/
  INDEX.md            MOC: watched entities, recent digests, recent research
  entities/<name>.md  frontmatter + profile + timeline with [[wikilinks]]
  digests/YYYY-MM-DD.md
  research/*.md       (written by agents/research.py)
Regenerates only when content hash changes. User notes belong in separate files that link here.
"""
from __future__ import annotations

import hashlib
import logging
import re
from datetime import date
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Digest, Entity
from ..services.entities import entity_overview
from ..settings import ROOT, get_settings

log = logging.getLogger(__name__)


def vault_dir() -> Path:
    base = get_settings().obsidian_dir
    return (Path(base) if base else ROOT / "obsidian-vault") / "TechRadar"


def _safe(name: str) -> str:
    """Filename-safe and wikilink-safe (Obsidian treats # ^ [ ] as link syntax)."""
    return re.sub(r'[\\/:*?"<>|#^\[\]]+', "-", name).strip("- ") or "unnamed"


def _write_if_changed(path: Path, content: str) -> bool:
    path.parent.mkdir(parents=True, exist_ok=True)
    h = hashlib.sha1(content.encode()).hexdigest()[:12]
    content = content.replace("{{HASH}}", h)
    if path.exists():
        old = path.read_text(encoding="utf-8")
        m = re.search(r"^generated_hash: (\w+)$", old, re.M)
        if m and m.group(1) == h:
            return False
    path.write_text(content, encoding="utf-8")
    return True


def render_entity(session: Session, e: Entity) -> str:
    ov = entity_overview(session, e)
    br = e.brief or {}
    lines = [
        "---",
        f"type: {ov['type']}",
        f"watched: {str(ov['watched']).lower()}",
        f"first_seen: {ov['first_seen'] or ''}",
    ]
    if br.get("trend"):
        # trend text starts with one of 升温/平稳/降温 — keep just that word for dataview queries
        word = next((w for w in ("升温", "平稳", "降温") if br["trend"].startswith(w)), None)
        if word:
            lines.append(f"trend: {word}")
    if e.brief_at:
        lines.append(f"brief_at: {e.brief_at.isoformat()[:10]}")
    lines += [
        "generated: techradar",
        "generated_hash: {{HASH}}",
        "---",
        "",
        f"# {ov['name']}",
        "",
    ]
    if br:
        lines += ["> [!abstract] 当前状态", f"> {br.get('status', '')}", ">"]
        if br.get("activity"):
            lines.append(f"> **最近**：{br['activity']}")
        if br.get("trend"):
            lines.append(f"> **趋势**：{br['trend']}")
        if br.get("advice"):
            lines.append(f"> **建议**：{br['advice']}")
        for h in br.get("highlights") or []:
            lines.append(f"> - {h}")
        lines.append("")
    if ov["anchors"]:
        lines.append(" · ".join(f"[{k}]({_anchor_url(k, v)})" for k, v in ov["anchors"].items()))
        lines.append("")
    if ov["notes"]:
        lines += ["> [!note] 备注", f"> {ov['notes']}", ""]
    rel = _related_pages(ov["name"])
    if rel["research"] or rel["notes"]:
        lines.append("## 相关笔记")
        lines += [f"- [[research/{p}|{p}]]" for p in rel["research"]]
        lines += [f"- [[notes/{p}|{p}]]" for p in rel["notes"]]
        lines.append("")
    lines.append("## 时间线")
    lines.append("")
    if not ov["timeline"]:
        lines.append("（暂无记录）")
    last_day = None
    for t in ov["timeline"]:
        if t["ts"] != last_day:
            lines.append(f"### {t['ts']}")
            last_day = t["ts"]
        rel = " ".join(f"[[{_safe(x)}]]" for x in t["entities"][:3])
        summary = t["summary"] or t["title"]
        lines.append(f"- **{t['event']}** [{summary[:80]}]({t['url']}) {rel}".rstrip())
    lines.append("")
    return "\n".join(lines)


def _related_pages(entity_name: str) -> dict:
    """Pages whose frontmatter lists this entity — gives entity pages a backlink section."""
    out = {"research": [], "notes": []}
    pat = re.compile(r"^entities:\s*\[(.*)\]", re.M)
    for sub in out:
        d = vault_dir() / sub
        if not d.exists():
            continue
        for p in sorted(d.glob("*.md"), reverse=True):
            try:
                text = p.read_text(encoding="utf-8")[:2000]
            except OSError:
                continue
            head = text[: text.find("\n---", 3)] if text.startswith("---") else text[:600]
            m = pat.search(head)
            if m and entity_name in [x.strip() for x in m.group(1).split(",")]:
                out[sub].append(p.stem)
    return out


GENERATED_RE = re.compile(r"^generated:\s*techradar\s*$", re.M)


def is_generated(text: str) -> bool:
    """True only when the marker sits in the leading frontmatter block.

    A hand-written note that merely *mentions* `generated: techradar` in its body must never be
    treated as ours — deleting user data is unrecoverable.
    """
    if not text.startswith("---"):
        return False
    end = text.find("\n---", 3)
    if end == -1:
        return False
    return bool(GENERATED_RE.search(text[3:end]))


def prune_orphans(session: Session, mocs: list[dict] | None = None) -> list[str]:
    """Remove generated pages whose source row is gone (e.g. a deleted digest). Never touches
    hand-written pages: only files carrying `generated: techradar` are considered."""
    from ..models import Digest, Entity
    root = vault_dir()
    keep_entities = {_safe(e.canonical_name) for e in session.scalars(select(Entity)).all()}
    keep_digests = {d.day.isoformat() + ("-weekly" if d.kind == "weekly" else "")
                    for d in session.scalars(select(Digest)).all()}
    keep_topics = {_safe(m["label"]) for m in (mocs or [])} if mocs else None
    removed = []
    plans = [("entities", keep_entities), ("digests", keep_digests)]
    if keep_topics is not None:
        plans.append(("topics", keep_topics))
    for sub, keep in plans:
        d = root / sub
        if not d.exists():
            continue
        existing = list(d.glob("*.md"))
        if not keep and existing:
            # empty keep-set means "we know of nothing" (wrong DB / failed query), not "delete all"
            log.warning("prune skipped %s: keep-set empty but %d files present", sub, len(existing))
            continue
        for p in existing:
            if p.stem in keep:
                continue
            try:
                if not is_generated(p.read_text(encoding="utf-8")):
                    continue                       # hand-written file: leave it alone
                p.unlink()
                removed.append(f"{sub}/{p.name}")
            except OSError as e:
                log.warning("prune failed %s: %s", p, e)
    return removed


def _anchor_url(kind: str, val: str) -> str:
    if kind == "github":
        return f"https://github.com/{val}"
    return val


def render_digest_md(dg: Digest) -> str:
    body = dg.markdown or ""
    return ("---\n"
            f"date: {dg.day.isoformat()}\n"
            "generated: techradar\n"
            "generated_hash: {{HASH}}\n"
            "---\n\n" + body + "\n")


def render_moc(moc: dict) -> str:
    n = moc.get("narrative") or {}
    lines = ["---", f"topic: {moc['topic']}", f"items_7d: {moc['count']}",
             "generated: techradar", "generated_hash: {{HASH}}", "---", "",
             f"# {moc['label']}", ""]
    if moc.get("queries"):
        lines += [f"*订阅词：{'、'.join(moc['queries'][:8])}*", ""]
    if n.get("summary"):
        lines += ["> [!abstract] 本周", f"> {n['summary']}"]
        for th in n.get("themes") or []:
            lines.append(f"> - {th}")
        lines.append("")
    by_id = {i["id"]: i for i in moc["items"]}
    if n.get("notable"):
        lines += ["## 值得点开", ""]
        for note in n["notable"]:
            # LLM may write "- #7 …", "**#7**", "条目 #7: …" or a bare "7 …"
            m = re.search(r"#(\d+)", note) or re.match(r"\s*[-*]?\s*(\d+)\b", note)
            if m and int(m.group(1)) in by_id:
                it = by_id[int(m.group(1))]
                reason = note[m.end():].lstrip(" :：,，-—*").strip(" *")
                lines.append(f"- [{it['summary'] or it['title']}]({it['url']})" + (f" — {reason}" if reason else ""))
            else:
                lines.append(f"- {note.strip()}")
        lines.append("")
    lines += ["## 本周条目", ""]
    if not moc["items"]:
        lines.append("（本周该主题没有新条目）")
    for it in moc["items"]:
        ents = " ".join(f"[[entities/{_safe(e)}|{e}]]" for e in it["entities"][:3])
        lines.append(f"- `{it['date']}` [{(it['summary'] or it['title'])[:80]}]({it['url']}) {ents}".rstrip())
    lines.append("")
    return "\n".join(lines)


def render_index(session: Session, ndigests: int = 14) -> str:
    ents = session.scalars(select(Entity).order_by(Entity.watched.desc(), Entity.canonical_name)).all()
    digests = session.scalars(select(Digest).order_by(Digest.day.desc()).limit(ndigests)).all()
    research = sorted((vault_dir() / "research").glob("*.md"), reverse=True)[:14] if (vault_dir() / "research").exists() else []
    mocs = sorted((vault_dir() / "topics").glob("*.md")) if (vault_dir() / "topics").exists() else []
    notes = sorted((vault_dir() / "notes").glob("*.md"), reverse=True)[:10] if (vault_dir() / "notes").exists() else []
    lines = ["---", "generated: techradar", "generated_hash: {{HASH}}", "---", "", "# TechRadar 索引", ""]
    if mocs:
        lines.append("## 主题地图")
        lines += [f"- [[topics/{p.stem}|{p.stem}]]" for p in mocs]
        lines.append("")
    lines.append("## 关注实体")
    for e in ents:
        star = "⭐ " if e.watched else ""
        lines.append(f"- {star}[[entities/{_safe(e.canonical_name)}|{e.canonical_name}]] ({e.type})")
    lines += ["", "## 最近日报"]
    lines += [f"- [[digests/{d.day.isoformat()}{'-weekly' if d.kind == 'weekly' else ''}|{d.day.isoformat()}{' 周报' if d.kind == 'weekly' else ''}]]" for d in digests]
    if research:
        lines += ["", "## 深挖报告"]
        lines += [f"- [[research/{p.stem}|{p.stem}]]" for p in research]
    if notes:
        lines += ["", "## 我的问答笔记"]
        lines += [f"- [[notes/{p.stem}|{p.stem}]]" for p in notes]
    lines.append("")
    return "\n".join(lines)


def render_all(session: Session, mocs: list[dict] | None = None, mocs_complete: bool = True) -> dict:
    out = {"entities": 0, "digests": 0, "topics": 0, "index": 0}
    root = vault_dir()
    for moc in mocs or []:
        if _write_if_changed(root / "topics" / f"{_safe(moc['label'])}.md", render_moc(moc)):
            out["topics"] += 1
    for e in session.scalars(select(Entity)).all():
        if _write_if_changed(root / "entities" / f"{_safe(e.canonical_name)}.md", render_entity(session, e)):
            out["entities"] += 1
    for dg in session.scalars(select(Digest).where(Digest.markdown.isnot(None))).all():
        name = dg.day.isoformat() + ("-weekly" if dg.kind == "weekly" else "")
        if _write_if_changed(root / "digests" / f"{name}.md", render_digest_md(dg)):
            out["digests"] += 1
    if _write_if_changed(root / "INDEX.md", render_index(session)):
        out["index"] = 1
    out["pruned"] = prune_orphans(session, mocs if mocs_complete else None)
    return out
