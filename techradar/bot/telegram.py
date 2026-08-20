"""Telegram front door (python-telegram-bot ≥21, tested on 22.x): daily digest push with inline buttons, callbacks → feedback,
plain text → search, /add /mute /web /inbox /dig. Uses python-telegram-bot v21 (async)."""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone

from ..db import session_scope
from ..settings import get_settings

log = logging.getLogger(__name__)
CB_RE = re.compile(r"^act:(save|ignore|dig)$")
NUM_RE = re.compile(r"^(?:(收藏|深挖|忽略|save|dig|ignore)\s*)?([\d\s,，、]+)$")
ACTION_ZH = {"save": "收藏", "dig": "深挖", "ignore": "忽略"}
ZH_ACTION = {"收藏": "save", "深挖": "dig", "忽略": "ignore", "save": "save", "dig": "dig", "ignore": "ignore"}


def _kb(item_ids: list[int] | None = None):
    """Three action buttons; the user then types digest numbers."""
    from telegram import InlineKeyboardButton, InlineKeyboardMarkup
    return InlineKeyboardMarkup([[
        InlineKeyboardButton("⭐ 收藏", callback_data="act:save"),
        InlineKeyboardButton("🔍 深挖", callback_data="act:dig"),
        InlineKeyboardButton("🙈 忽略", callback_data="act:ignore"),
    ]])


def _split(text: str, limit: int = 3900) -> list[str]:
    parts, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > limit:
            parts.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        parts.append(cur)
    return parts


def _html_to_plain(text: str) -> str:
    import html as _h
    text = re.sub(r'<a href="([^"]+)">(.*?)</a>', r"\2 \1", text)
    return _h.unescape(re.sub(r"<[^>]+>", "", text))


PENDING_TTL_S = 900


def _set_pending(context, action: str):
    import time
    context.chat_data["pending_action"] = action
    context.chat_data["pending_ts"] = time.time()


def _get_pending(context) -> str | None:
    import time
    a = context.chat_data.get("pending_action")
    if a and time.time() - context.chat_data.get("pending_ts", 0) > PENDING_TTL_S:
        context.chat_data.pop("pending_action", None)
        return None
    return a


def _no_preview():
    from telegram import LinkPreviewOptions
    return LinkPreviewOptions(is_disabled=True)


async def _send_digest_async(html_text: str, top_ids: list[int]) -> bool:
    from telegram import Bot
    from telegram.constants import ParseMode
    s = get_settings()
    if not (s.telegram_bot_token and s.telegram_chat_id):
        log.error("telegram not configured")
        return False
    bot = Bot(s.telegram_bot_token)
    parts = _split(html_text)
    for i, p in enumerate(parts):
        kb = _kb() if i == len(parts) - 1 else None
        try:
            await bot.send_message(chat_id=s.telegram_chat_id, text=p, parse_mode=ParseMode.HTML,
                                   link_preview_options=_no_preview(), reply_markup=kb)
        except Exception as e:  # parse errors → fallback plain (keep urls)
            log.warning("html send failed (%s); retrying plain", e)
            plain = _html_to_plain(p)
            await bot.send_message(chat_id=s.telegram_chat_id, text=plain, link_preview_options=_no_preview(), reply_markup=kb)
    return True


def send_digest(html_text: str, d) -> bool:
    """`html_text` should come from render_markdown(d, html=True)."""
    return asyncio.run(_send_digest_async(html_text, [it.id for it in d.top]))


def send_text_html(text: str) -> bool:
    async def _go():
        from telegram import Bot
        from telegram.constants import ParseMode
        s = get_settings()
        for part in _split(text):
            await Bot(s.telegram_bot_token).send_message(chat_id=s.telegram_chat_id, text=part,
                                                         parse_mode=ParseMode.HTML, link_preview_options=_no_preview())
        return True
    return asyncio.run(_go())


def send_text(text: str) -> bool:
    async def _go():
        from telegram import Bot
        s = get_settings()
        await Bot(s.telegram_bot_token).send_message(chat_id=s.telegram_chat_id, text=text, link_preview_options=_no_preview())
        return True
    return asyncio.run(_go())


# ---------------- interactive bot -----------------
def _authorized(update) -> bool:
    cid = str(update.effective_chat.id) if update.effective_chat else ""
    return cid == str(get_settings().telegram_chat_id)


async def on_callback(update, context):
    q = update.callback_query
    if not _authorized(update):
        await q.answer("unauthorized"); return
    m = CB_RE.match(q.data or "")
    if not m:
        await q.answer(); return
    action = m.group(1)
    _set_pending(context, action)
    await q.answer()
    await context.bot.send_message(chat_id=update.effective_chat.id,
                                   text=f"要{ACTION_ZH[action]}哪几条？回复编号，如「3」或「1 3 5」")


async def _apply_numbers(update, context, action: str, numbers: list[int]):
    from ..digest.daily import resolve_positions
    from ..services.feedback import record_feedback
    from ..services.research import enqueue_research
    done, missing, hidden = [], [], 0
    with session_scope() as s:
        mapping = resolve_positions(s, numbers)
        for n, iid in mapping.items():
            if iid is None:
                missing.append(n); continue
            if action == "dig":
                enqueue_research(s, item_id=iid)
                record_feedback(s, iid, "dig", channel="telegram")
            else:
                r = record_feedback(s, iid, action, channel="telegram")
                hidden += r.get("hidden_similar", 0)
            done.append(n)
    parts = []
    if done:
        parts.append(f"已{ACTION_ZH[action]} {', '.join(map(str, done))}")
        if action == "ignore" and hidden:
            parts.append(f"另隐藏 {hidden} 条相似")
        if action == "dig":
            parts.append("完成后会推送报告")
    if missing:
        parts.append(f"没有编号 {', '.join(map(str, missing))}")
    context.chat_data.pop("pending_action", None)
    await update.message.reply_text("；".join(parts) or "没有可处理的编号")


async def on_text(update, context):
    if not _authorized(update):
        return
    text = (update.message.text or "").strip()
    if not text:
        return
    mid = re.match(r"^(收藏|深挖|忽略|save|dig|ignore)\s*#(\d+)$", text)
    if mid:   # "深挖 #1031" → direct item id (from /search output)
        action, iid = ZH_ACTION[mid.group(1)], int(mid.group(2))
        from ..services.feedback import record_feedback
        from ..services.research import enqueue_research
        try:
            with session_scope() as s:
                if action == "dig":
                    enqueue_research(s, item_id=iid)
                record_feedback(s, iid, action, channel="telegram")
        except KeyError:
            await update.message.reply_text(f"没有 #{iid} 这条"); return
        await update.message.reply_text(f"已{ACTION_ZH[action]} #{iid}" + ("，完成后推送报告" if action == "dig" else ""))
        return
    m = NUM_RE.match(text)
    if m:
        action = ZH_ACTION.get(m.group(1) or "") or _get_pending(context)
        numbers = [int(x) for x in re.split(r"[\s,，、]+", m.group(2).strip()) if x.isdigit()]
        if action and numbers:
            await _apply_numbers(update, context, action, numbers)
            return
        if numbers and not action:
            await update.message.reply_text("先点日报下方的按钮选择操作，或直接发「收藏 3」「深挖 5」「忽略 2」")
            return
    # anything else = ask the assistant (grounded on local memory)
    context.chat_data.pop("pending_action", None)
    await _answer(update, text)


async def cmd_search(update, context):
    if not _authorized(update):
        return
    text = " ".join(context.args).strip()
    if not text:
        await update.message.reply_text("用法: /search 关键词"); return
    from ..services.items import search_items
    with session_scope() as s:
        rows = search_items(s, text, limit=8)
    if not rows:
        await update.message.reply_text("没搜到。试试换个词，或 /add <url> 投喂。")
        return
    import html as _h
    from telegram.constants import ParseMode
    from ..digest.daily import SRC_ABBR, _feed_labels
    labels = _feed_labels()
    lines = [f"🔎 “{_h.escape(text)}” 找到 {len(rows)} 条："]
    for i, r in enumerate(rows, 1):
        srcs = "+".join(sorted({labels.get(x["source"]) or SRC_ABBR.get(x["source"].split(":")[0], x["source"].split(":")[0]) for x in r["sources"]}))
        intro = (r.get("summary_one") or r["title"]).rstrip("。")[:80]
        lines.append(f'{i}. <a href="{_h.escape(r["url"], quote=True)}">{_h.escape(intro)}</a> [{_h.escape(srcs)}] · {r["first_seen_at"][:10]} · #{r["id"]}')
    lines.append("\n回复「深挖 #编号」可深挖其中一条")
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, link_preview_options=_no_preview())


async def _answer(update, question: str, web: bool | None = None):
    from ..agents.chat import ask
    from telegram.constants import ParseMode
    import html as _h
    def _run():
        with session_scope() as s:
            from ..services.chatlog import recent_turns, record_turn
            history = recent_turns(s, limit=3, within_minutes=45)
            r = ask(s, question, history=history, web=web)
            if r.get("citations"):
                record_turn(s, question, r)
            return r
    try:
        await update.message.chat.send_action("typing")
        r = await asyncio.to_thread(_run)
    except Exception as e:  # noqa: BLE001
        log.exception("ask failed")
        await update.message.reply_text(f"回答失败：{type(e).__name__}")
        return
    lines = [_h.escape(r["answer"])]
    if r["citations"]:
        lines.append("")
        for c in r["citations"]:
            tag = "🌐" if c.get("kind") == "web" else "📁"
            lines.append(f'{tag} [{c["n"]}] <a href="{_h.escape(c["url"], quote=True)}">{_h.escape(c["title"][:60])}</a>')
    if r.get("web_count"):
        lines.append(f'<i>联网检索 {r["web_count"]} 条</i>')
    await update.message.reply_text("\n".join(lines), parse_mode=ParseMode.HTML, link_preview_options=_no_preview())


async def cmd_ask(update, context):
    if not _authorized(update):
        return
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("用法: /ask 你的问题（直接发文字也可以）"); return
    await _answer(update, q)


async def cmd_web(update, context):
    """Force a live web lookup (Brave / GitHub / HN / arXiv) even when local memory has hits."""
    if not _authorized(update):
        return
    q = " ".join(context.args).strip()
    if not q:
        await update.message.reply_text("用法: /web 问题或链接 — 强制联网检索并读取网页"); return
    await _answer(update, q, web=True)


async def cmd_add(update, context):
    if not _authorized(update):
        return
    if not context.args:
        await update.message.reply_text("用法: /add <url> [备注]"); return
    url, note = context.args[0], " ".join(context.args[1:]) or None
    from ..services.manual import add_url
    with session_scope() as s:
        r = add_url(s, url, note=note)
    await update.message.reply_text(f"已加入 #{r['id']}: {r['title'][:60]}")


async def cmd_mute(update, context):
    if not _authorized(update):
        return
    if not context.args:
        await update.message.reply_text("用法: /mute <source|tag:xxx|author:src:key> [days]"); return
    key = context.args[0]
    days = int(context.args[1].rstrip("d")) if len(context.args) > 1 else 7
    kind = "source"
    if ":" in key and key.split(":")[0] in ("tag", "author", "entity"):
        kind, key = key.split(":", 1)
    from ..services.feedback import mute
    with session_scope() as s:
        r = mute(s, kind, key, days)
    await update.message.reply_text(f"已静音 {kind}:{key} 至 {r['muted_until'][:10]}")


async def cmd_inbox(update, context):
    if not _authorized(update):
        return
    from ..services.feedback import list_inbox
    with session_scope() as s:
        rows = list_inbox(s, limit=15)
    if not rows:
        await update.message.reply_text("收藏夹为空"); return
    await update.message.reply_text("\n".join(f"#{r['id']} {r['title'][:60]}\n   {r['url']}" for r in rows),
                                    link_preview_options=_no_preview())


async def cmd_dig(update, context):
    if not _authorized(update):
        return
    if not context.args or not context.args[0].lstrip("#").isdigit():
        await update.message.reply_text("用法: /dig <item_id> [问题]"); return
    n = int(context.args[0].lstrip("#"))
    question = " ".join(context.args[1:]) or None
    from ..digest.daily import resolve_positions
    from ..services.research import enqueue_research
    from ..services.feedback import record_feedback
    with session_scope() as s:
        iid = resolve_positions(s, [n]).get(n) if n <= 50 else n   # small numbers = digest position
        if iid is None:
            await update.message.reply_text(f"日报里没有编号 {n}"); return
        t = enqueue_research(s, item_id=iid, question=question)
        record_feedback(s, iid, "dig", channel="telegram")
    await update.message.reply_text(f"已入队深挖第 {n} 条（任务 {t['task_id']}），完成后推送。")


async def cmd_start(update, context):
    await update.message.reply_text(
        f"你好，这里是 TechRadar 📡\nchat_id = {update.effective_chat.id}\n\n"
        "• 每天早上推送技术日报；点底部按钮再回复编号即可 ⭐收藏 / 🔍深挖 / 🙈忽略，或直接发「收藏 3」\n"
        "• 直接发文字 = 提问（本地资料不足会自动联网）；贴 URL 它会直接读\n"
        "• /web 问题 = 强制联网检索；/search 关键词 = 只列条目\n"
        "• /inbox 收藏夹 · /today 重发日报 · /add <url> 投喂 · /mute <source> 静音 · /health 源状态")


COMMANDS = [
    ("start", "显示 chat_id 与使用说明"),
    ("inbox", "查看收藏夹"),
    ("search", "关键词检索条目列表：/search vllm"),
    ("web", "强制联网检索并读网页：/web 问题或链接"),
    ("add", "投喂一个链接：/add <url> [备注]"),
    ("dig", "深挖某条：/dig <编号> [问题]"),
    ("mute", "静音来源/标签：/mute <source> [天数]"),
    ("today", "重发今天的日报"),
    ("health", "查看数据源健康状态"),
]


async def _set_commands(app):
    from telegram import BotCommand, BotCommandScopeAllPrivateChats, BotCommandScopeDefault
    cmds = [BotCommand(c, d) for c, d in COMMANDS]
    # private-chat scope overrides default scope; a stale private-scope list (e.g. from a previous bot
    # framework on the same token) would shadow ours — set both.
    await app.bot.set_my_commands(cmds, scope=BotCommandScopeDefault())
    await app.bot.set_my_commands(cmds, scope=BotCommandScopeAllPrivateChats())
    await app.bot.set_my_description("TechRadar 个人技术情报台：每天推送技术日报，直接发文字即可搜索历史条目。")
    await app.bot.set_my_short_description("个人技术情报日报 + 检索")


async def cmd_today(update, context):
    if not _authorized(update):
        return
    from ..digest.daily import ensure_enriched, load_persisted, persist_digest, render_markdown, select_digest
    from telegram.constants import ParseMode
    def _build():
        with session_scope() as s:
            d = load_persisted(s)
            if d is None:
                d = select_digest(s)
                ensure_enriched(s, d)
                persist_digest(s, d, render_markdown(d), sent=True)
            return render_markdown(d, html=True)
    text = await asyncio.to_thread(_build)
    parts = _split(text)
    for i, p in enumerate(parts):
        kb = _kb() if i == len(parts) - 1 else None
        await update.message.reply_text(p, parse_mode=ParseMode.HTML, link_preview_options=_no_preview(), reply_markup=kb)


async def cmd_health(update, context):
    if not _authorized(update):
        return
    from ..services.health import list_sources_health
    with session_scope() as s:
        rows = list_sources_health(s)
    lines = [f"{'✅' if r['consecutive_failures'] == 0 else '❌'} {r['source']}: {r['last_items'] or 0} 条 · 失败 {r['consecutive_failures']} · 月调用 {r['month_calls']}"
             for r in rows]
    await update.message.reply_text("\n".join(lines) or "暂无数据")


TRANSIENT_NET_ERRORS = ("RemoteProtocolError", "ConnectError", "ReadError", "TimedOut", "NetworkError", "ConnectTimeout", "PoolTimeout")


async def on_error(update, context):
    name = type(context.error).__name__
    cause = type(context.error.__cause__).__name__ if getattr(context.error, "__cause__", None) else ""
    if update is None and (name in TRANSIENT_NET_ERRORS or cause in TRANSIENT_NET_ERRORS):
        log.warning("transient network error (auto-retry): %s/%s", name, cause)
        return
    log.exception("bot error: %s", context.error)
    try:
        if update and getattr(update, "effective_chat", None):
            context.chat_data.pop("pending_action", None)
            await context.bot.send_message(chat_id=update.effective_chat.id,
                                           text=f"出错了：{type(context.error).__name__}，已记录日志。")
    except Exception:  # noqa: BLE001
        pass


_WD = {"fails": 0, "last_ok": 0.0}


async def _watchdog(context):
    """Self-heal: if Telegram is unreachable for ~5 consecutive probes, exit so the supervisor restarts us."""
    import os, time
    try:
        await context.bot.get_me(read_timeout=15, connect_timeout=10)
        _WD["fails"] = 0
        _WD["last_ok"] = time.time()
    except Exception as e:  # noqa: BLE001
        _WD["fails"] += 1
        log.warning("watchdog probe failed (%s/5): %s", _WD["fails"], type(e).__name__)
        if _WD["fails"] >= 5:
            log.error("watchdog: telegram unreachable for 5 probes — exiting for restart")
            os._exit(3)


async def _post_init(app):
    await _set_commands(app)
    if app.job_queue:
        app.job_queue.run_repeating(_watchdog, interval=60, first=60, name="watchdog")


def build_app():
    from telegram.ext import Application, CallbackQueryHandler, CommandHandler, MessageHandler, filters
    from telegram.request import HTTPXRequest
    s = get_settings()
    req = HTTPXRequest(connection_pool_size=8, read_timeout=30, write_timeout=30, connect_timeout=15, pool_timeout=10)
    get_req = HTTPXRequest(connection_pool_size=2, read_timeout=40, connect_timeout=15)
    app = (Application.builder().token(s.telegram_bot_token).request(req).get_updates_request(get_req)
           .post_init(_post_init).build())
    app.add_error_handler(on_error)
    app.add_handler(CommandHandler("ask", cmd_ask))
    app.add_handler(CommandHandler("search", cmd_search))
    app.add_handler(CommandHandler("web", cmd_web))
    app.add_handler(CommandHandler("today", cmd_today))
    app.add_handler(CommandHandler("health", cmd_health))
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("add", cmd_add))
    app.add_handler(CommandHandler("mute", cmd_mute))
    app.add_handler(CommandHandler("inbox", cmd_inbox))
    app.add_handler(CommandHandler("dig", cmd_dig))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    return app


def run_polling():
    app = build_app()
    app.run_polling(drop_pending_updates=True, timeout=30, bootstrap_retries=-1)
