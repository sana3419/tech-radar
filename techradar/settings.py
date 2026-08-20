from __future__ import annotations

from functools import lru_cache
from pathlib import Path

import yaml
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = ROOT / "config"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="TECHRADAR_", env_file=ROOT / ".env", extra="ignore")

    database_url: str = "postgresql+psycopg://techradar:techradar@localhost:5433/techradar"
    github_token: str | None = None
    # LLM provider: "openai" (any OpenAI-compatible endpoint: DeepSeek/OpenAI/gateway) or "anthropic"
    llm_provider: str = "openai"
    openai_api_key: str | None = None
    openai_base_url: str | None = "https://api.deepseek.com"
    llm_model_enrich: str = "deepseek-chat"
    llm_model_research: str = "deepseek-reasoner"
    anthropic_api_key: str | None = None
    telegram_bot_token: str | None = None
    telegram_chat_id: str | None = None
    llm_daily_budget_usd: float = 1.0
    digest_hour: int = 8
    timezone: str = "Asia/Shanghai"
    config_dir: Path = CONFIG_DIR
    obsidian_dir: str | None = None
    # optional general web search (vertical APIs work without either)
    brave_api_key: str | None = None
    tavily_api_key: str | None = None
    web_token: str | None = None         # when set, web requires ?token=... once (cookie afterwards)      # optional: Obsidian vault path; research reports go to <vault>/TechRadar/research/
    # digest limits (hard constraints from requirements)
    digest_top_n: int = 8
    digest_folded_n: int = 10
    unread_expire_hours: int = 48


class TopicSub(BaseModel):
    name: str
    label: str | None = None
    queries: list[str]
    boost: float = 1.0
    sources: list[str] | None = None


class AuthorSub(BaseModel):
    source: str
    key: str
    weight: float = 1.0


class EntitySub(BaseModel):
    name: str
    type: str = "project"
    aliases: list[str] = Field(default_factory=list)
    anchors: dict[str, str] = Field(default_factory=dict)


class Subscriptions(BaseModel):
    topics: list[TopicSub] = Field(default_factory=list)
    authors: list[AuthorSub] = Field(default_factory=list)
    entities: list[EntitySub] = Field(default_factory=list)
    sources: dict = Field(default_factory=dict)


@lru_cache
def get_settings() -> Settings:
    return Settings()


def _load_yaml(name: str) -> dict:
    """Prefer `<name>.local.yaml` when present: it is gitignored, so a public checkout ships the
    example config while your real subscriptions stay out of the repo."""
    d = get_settings().config_dir
    stem, _, ext = name.rpartition(".")
    local = d / f"{stem}.local.{ext}"
    p = local if local.exists() else d / name
    if not p.exists():
        return {}
    with p.open("r", encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


@lru_cache
def get_subscriptions() -> Subscriptions:
    return Subscriptions.model_validate(_load_yaml("subscriptions.yaml"))


@lru_cache
def get_taxonomy() -> dict[str, list[str]]:
    return _load_yaml("taxonomy.yaml")


@lru_cache
def get_profile() -> dict[str, dict[str, float]]:
    return _load_yaml("profile.yaml")
