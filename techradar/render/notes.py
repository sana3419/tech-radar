"""User-facing notes written into the vault (unlike generated pages, these are yours to edit).

A saved Q&A becomes <vault>/TechRadar/notes/YYYY-MM-DD-<slug>.md with wikilinks to every entity
and cited item, so answers accumulate into the knowledge base instead of vanishing with the page.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy.orm import Session

from ..models import Item
from .obsidian import _safe, vault_dir


def _slug(text: str, n: int = 48) -> str:
    s = re.sub(r"[^\w一-鿿]+", "-", text).strip("-")
    return (s[:n] or "note").rstrip("-")


def save_answer(session: Session, question: str, answer: str, citations: list[dict]) -> Path:
    day = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    path = vault_dir() / "notes" / f"{day}-{_slug(question)}.md"
    path.parent.mkdir(parents=True, exist_ok=True)

    entities: list[str] = []
    for c in citations:
        it = session.get(Item, c.get("id"))
        if it:
            for e in it.entities_matched or []:
                if e not in entities:
                    entities.append(e)

    lines = ["---", f"date: {day}", "source: techradar-ask", "tags: [techradar/qa]"]
    if entities:
        lines.append("entities: [" + ", ".join(entities) + "]")
    lines += ["---", "", f"# {question}", "", answer, ""]
    if citations:
        lines += ["## 出处", ""]
        for c in citations:
            lines.append(f"- [{c['n']}] [{c['title'][:80]}]({c['url']})")
        lines.append("")
    if entities:
        lines += ["## 相关实体", "", " · ".join(f"[[entities/{_safe(e)}|{e}]]" for e in entities), ""]
    lines += ["## 我的笔记", "", "", ""]      # left for the human to fill in; never regenerated

    if path.exists():                          # never clobber a note the user may have edited
        path = path.with_name(f"{path.stem}-{datetime.now(timezone.utc).strftime('%H%M%S')}.md")
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
