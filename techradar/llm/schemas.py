"""Structured output schemas for LLM calls (closed enums come from config/taxonomy.yaml)."""
from __future__ import annotations

from pydantic import BaseModel, Field


class EnrichOut(BaseModel):
    summary_one: str = Field(description="≤40 字中文一句话，说明这是什么、为什么值得看")
    points: list[str] = Field(description="2-3 条中文要点，每条 ≤30 字", min_length=1, max_length=3)
    type: str = Field(description="release|tool|paper|opinion|tutorial|incident|other")
    domains: list[str] = Field(description="从给定 domains 枚举中选 1-3 个", max_length=3)
    stacks: list[str] = Field(description="从给定 stacks 枚举中选 0-3 个", max_length=3)
    entities: list[str] = Field(description="只能从候选实体列表中选，没有则为空", default_factory=list)
    lang: str = Field(description="原文语言 zh|en|other")


class EnrichBatchOut(BaseModel):
    items: list[EnrichOut]


class ResearchOut(BaseModel):
    tldr: str
    should_follow: str = Field(description="是|否|观望 + 一句理由")
    key_facts: list[str]
    relation_to_known: list[str] = Field(default_factory=list, description="与本地已知条目/实体的关系，引用 #item_id 或实体名")
    risks: list[str] = Field(default_factory=list)
    sources: list[str] = Field(default_factory=list, description="用到的 URL")


class EntityBriefOut(BaseModel):
    """Agent-written 'current state' card shown at the top of an entity page."""

    status: str = Field(description="≤80 字中文：这个项目/技术现在处于什么状态（成熟度、最新版本、能力边界）")
    activity: str = Field(description="≤60 字中文：最近在做什么（近期变更、讨论焦点）")
    trend: str = Field(description="升温|平稳|降温 + 一句依据（≤30 字）")
    advice: str = Field(description="≤50 字中文：对一位后端/AI 开发者的跟进建议")
    highlights: list[str] = Field(default_factory=list, max_length=3,
                                  description="0-3 条最值得知道的具体事实，每条 ≤30 字")


class TopicMocOut(BaseModel):
    """Agent-written weekly narrative for a subscription topic MOC page."""

    summary: str = Field(description="≤120 字中文：本周该主题发生了什么，有什么变化")
    themes: list[str] = Field(default_factory=list, max_length=4,
                              description="1-4 条本周的子主题/趋势，每条 ≤25 字")
    notable: list[str] = Field(default_factory=list, max_length=3,
                               description="0-3 条最值得点开的条目，写成 '#id 一句话理由'")
