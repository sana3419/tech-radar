"""Unified LLM layer: provider-agnostic structured outputs, usage accounting, daily budget.

Providers:
- "openai": any OpenAI-compatible Chat Completions endpoint (DeepSeek, OpenAI, gateways). Structured
  output via response_format json_object + schema in system prompt + pydantic validation (1 retry).
- "anthropic": Claude messages.parse (kept for optional use).
All calls in the project go through `structured()`.
"""
from __future__ import annotations

import json
import logging
from datetime import date
from typing import TypeVar

from pydantic import BaseModel, ValidationError
from sqlalchemy.orm import Session

from ..models import LlmUsage
from ..settings import get_settings

log = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)

# USD per 1M tokens: (input, output, cached_input). Approximate; update as pricing changes.
PRICES = {
    "deepseek-chat": (0.27, 1.10, 0.07),
    "deepseek-reasoner": (0.55, 2.19, 0.14),
    "gpt-4o-mini": (0.15, 0.60, 0.075),
    "gpt-4o": (2.50, 10.00, 1.25),
    "gpt-4.1-mini": (0.40, 1.60, 0.10),
    "claude-haiku-4-5": (1.00, 5.00, 0.10),
    "claude-sonnet-5": (3.00, 15.00, 0.30),
    "claude-opus-5": (5.00, 25.00, 0.50),
}


def model_enrich() -> str:
    return get_settings().llm_model_enrich


def model_research() -> str:
    return get_settings().llm_model_research


class BudgetExceeded(RuntimeError):
    pass


class LLMNotConfigured(RuntimeError):
    pass


def is_configured() -> bool:
    s = get_settings()
    return bool(s.openai_api_key) if s.llm_provider == "openai" else bool(s.anthropic_api_key)


def cost_usd(model: str, tokens_in: int, tokens_out: int, cache_read: int = 0, cache_write: int = 0) -> float:
    pi, po, pc = PRICES.get(model, (1.0, 4.0, 0.1))
    return tokens_in / 1e6 * pi + tokens_out / 1e6 * po + cache_read / 1e6 * pc + cache_write / 1e6 * pi * 1.25


def _local_today() -> date:
    from datetime import datetime
    from zoneinfo import ZoneInfo
    return datetime.now(ZoneInfo(get_settings().timezone)).date()


def today_usage(session: Session) -> LlmUsage:
    d = _local_today()
    u = session.get(LlmUsage, d)
    if u is None:
        u = LlmUsage(day=d, calls=0, tokens_in=0, tokens_out=0, cost_usd=0)
        session.add(u)
        session.flush()
    return u


def budget_remaining(session: Session) -> float:
    """Re-reads the row so a long-lived session doesn't guard against a stale (cached) spend."""
    u = today_usage(session)
    session.expire(u)
    return get_settings().llm_daily_budget_usd - float(u.cost_usd or 0)


def record_usage(session: Session, model: str, tokens_in: int, tokens_out: int,
                 cache_read: int = 0, cache_write: int = 0) -> float:
    """Atomic increment: read-modify-write would lose updates when several jobs bill concurrently."""
    from decimal import Decimal

    from sqlalchemy import update
    u = today_usage(session)
    c = cost_usd(model, tokens_in, tokens_out, cache_read, cache_write)
    session.execute(
        update(LlmUsage).where(LlmUsage.day == u.day).values(
            calls=LlmUsage.calls + 1,
            tokens_in=LlmUsage.tokens_in + tokens_in + cache_read + cache_write,
            tokens_out=LlmUsage.tokens_out + tokens_out,
            # Decimal, not float: the column is NUMERIC and ORM-side synchronisation would
            # otherwise try Decimal + float in Python and raise
            cost_usd=LlmUsage.cost_usd + Decimal(str(round(c, 6))),
        ),
        execution_options={"synchronize_session": False},
    )
    session.expire(u)
    return c


# ---------------- providers ----------------
def _openai_client():
    from openai import OpenAI
    s = get_settings()
    if not s.openai_api_key:
        raise LLMNotConfigured("TECHRADAR_OPENAI_API_KEY not set")
    return OpenAI(api_key=s.openai_api_key, base_url=s.openai_base_url or None, timeout=120, max_retries=4)


def _schema_hint(schema: type[BaseModel]) -> str:
    return ("\n\n输出必须是且仅是一个 JSON 对象，严格符合以下 JSON Schema（不要 markdown 代码块，不要额外文字）：\n"
            + json.dumps(schema.model_json_schema(), ensure_ascii=False))


def _openai_structured(session: Session, schema: type[T], system: str, user: str, model: str,
                       max_tokens: int, effort: str | None) -> tuple[T, dict]:
    client = _openai_client()
    sys_text = system + _schema_hint(schema)
    messages = [{"role": "system", "content": sys_text}, {"role": "user", "content": user}]
    last_err: Exception | None = None
    total_meta = {"model": model, "tokens_in": 0, "tokens_out": 0, "cost": 0.0, "cache_read": 0}
    for attempt in range(2):
        kwargs = {"model": model, "messages": messages, "max_tokens": max_tokens,
                  "response_format": {"type": "json_object"}}
        if not model.endswith("reasoner"):
            kwargs["temperature"] = 0.2
        try:
            resp = client.chat.completions.create(**kwargs)
        except Exception as e:  # transient network/5xx beyond the SDK's own retries
            import openai
            if attempt == 0 and isinstance(e, (openai.APIConnectionError, openai.APITimeoutError,
                                               openai.InternalServerError)):
                log.warning("LLM call failed (%s), retrying once", type(e).__name__)
                last_err = e
                continue
            raise
        u = resp.usage
        cached = 0
        details = getattr(u, "prompt_tokens_details", None)
        if details is not None:
            cached = getattr(details, "cached_tokens", 0) or 0
        # DeepSeek exposes prompt_cache_hit_tokens
        cached = cached or getattr(u, "prompt_cache_hit_tokens", 0) or 0
        ti = (u.prompt_tokens or 0) - cached
        to = u.output_tokens if hasattr(u, "output_tokens") else (u.completion_tokens or 0)
        c = record_usage(session, model, ti, to, cache_read=cached)
        total_meta["tokens_in"] += ti + cached
        total_meta["tokens_out"] += to
        total_meta["cost"] += c
        total_meta["cache_read"] += cached
        choice = resp.choices[0]
        text = choice.message.content or ""
        if choice.finish_reason == "length":
            log.warning("LLM hit max_tokens=%s; output truncated", max_tokens)
        try:
            data = json.loads(_strip_fence(text))
            return schema.model_validate(data), total_meta
        except (json.JSONDecodeError, ValidationError) as e:
            last_err = e
            log.warning("structured parse failed (attempt %s): %s", attempt + 1, str(e)[:200])
            messages = messages + [
                {"role": "assistant", "content": text[:4000]},
                {"role": "user", "content": f"上面的输出不符合 schema：{str(e)[:500]}。请只输出修正后的 JSON 对象。"},
            ]
    raise RuntimeError(f"structured output failed after retry: {last_err}")


def _strip_fence(text: str) -> str:
    t = text.strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[1] if "\n" in t else t[3:]
        if t.endswith("```"):
            t = t[:-3]
    return t.strip()


def _anthropic_structured(session: Session, schema: type[T], system: str, user: str, model: str,
                          max_tokens: int, effort: str | None) -> tuple[T, dict]:
    import anthropic
    s = get_settings()
    if not s.anthropic_api_key:
        raise LLMNotConfigured("TECHRADAR_ANTHROPIC_API_KEY not set")
    client = anthropic.Anthropic(api_key=s.anthropic_api_key)
    sys_block = [{"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}]
    kwargs = {"output_config": {"effort": effort}} if effort else {}
    resp = client.messages.parse(model=model, max_tokens=max_tokens, system=sys_block,
                                 messages=[{"role": "user", "content": user}], output_format=schema, **kwargs)
    cr = getattr(resp.usage, "cache_read_input_tokens", 0) or 0
    cw = getattr(resp.usage, "cache_creation_input_tokens", 0) or 0
    ti, to = resp.usage.input_tokens, resp.usage.output_tokens
    c = record_usage(session, model, ti, to, cr, cw)
    if resp.stop_reason == "refusal":
        raise RuntimeError("model refused")
    if resp.parsed_output is None:
        raise RuntimeError("no parsed output")
    return resp.parsed_output, {"model": model, "tokens_in": ti + cr + cw, "tokens_out": to, "cost": c, "cache_read": cr}


def structured(session: Session, schema: type[T], *, system: str, user: str, model: str | None = None,
               max_tokens: int = 4000, effort: str | None = None) -> tuple[T, dict]:
    """One structured call → (parsed, meta{model,tokens_in,tokens_out,cost,cache_read}).
    Raises BudgetExceeded / LLMNotConfigured / RuntimeError."""
    if budget_remaining(session) <= 0:
        raise BudgetExceeded("daily LLM budget exhausted")
    s = get_settings()
    model = model or model_enrich()
    if s.llm_provider == "anthropic":
        return _anthropic_structured(session, schema, system, user, model, max_tokens, effort)
    return _openai_structured(session, schema, system, user, model, max_tokens, effort)
