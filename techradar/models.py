"""SQLAlchemy models — mirrors docs/02-architecture.md §2."""
from __future__ import annotations

from datetime import datetime, date

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, Float, ForeignKey, Index, Integer, Numeric, SmallInteger,
    String, Text, UniqueConstraint, func, text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

EMBED_DIM = 1024


class Base(DeclarativeBase):
    pass


class Item(Base):
    __tablename__ = "items"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    canonical_key: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    kind: Mapped[str] = mapped_column(Text, nullable=False, default="other")
    title: Mapped[str] = mapped_column(Text, nullable=False)
    url: Mapped[str] = mapped_column(Text, nullable=False)
    lang: Mapped[str | None] = mapped_column(Text)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    content: Mapped[str | None] = mapped_column(Text)
    content_level: Mapped[int] = mapped_column(SmallInteger, default=0)
    status: Mapped[str] = mapped_column(Text, nullable=False, default="new")

    summary_one: Mapped[str | None] = mapped_column(Text)
    summary_points: Mapped[list | None] = mapped_column(JSONB)
    tags: Mapped[dict | None] = mapped_column(JSONB)
    entities_matched: Mapped[list | None] = mapped_column(JSONB)
    enrich_model: Mapped[str | None] = mapped_column(Text)
    enrich_version: Mapped[str | None] = mapped_column(Text)
    embedding = mapped_column(Vector(EMBED_DIM), nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(Text)

    score: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    ranker_version: Mapped[str | None] = mapped_column(Text)
    reasons: Mapped[list | None] = mapped_column(JSONB)
    event_id: Mapped[int | None] = mapped_column(BigInteger)

    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    sources: Mapped[list["ItemSource"]] = relationship(back_populates="item", cascade="all, delete-orphan")

    __table_args__ = (
        Index("ix_items_status_score", "status", text("score DESC")),
        Index("ix_items_first_seen", text("first_seen_at DESC")),
        Index(
            "ix_items_fts",
            text("to_tsvector('simple', coalesce(title,'') || ' ' || coalesce(summary_one,''))"),
            postgresql_using="gin",
        ),
    )


class ItemSource(Base):
    __tablename__ = "item_sources"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    source: Mapped[str] = mapped_column(Text, nullable=False)
    external_id: Mapped[str] = mapped_column(Text, nullable=False)
    source_url: Mapped[str | None] = mapped_column(Text)
    author: Mapped[str | None] = mapped_column(Text)
    author_key: Mapped[str | None] = mapped_column(Text)
    metrics_raw: Mapped[dict | None] = mapped_column(JSONB)
    seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    raw: Mapped[dict | None] = mapped_column(JSONB)

    item: Mapped[Item] = relationship(back_populates="sources")
    __table_args__ = (UniqueConstraint("source", "external_id", name="uq_item_sources_source_ext"),)


class Snapshot(Base):
    __tablename__ = "snapshots"
    item_source_id: Mapped[int] = mapped_column(
        ForeignKey("item_sources.id", ondelete="CASCADE"), primary_key=True
    )
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    metrics: Mapped[dict] = mapped_column(JSONB, nullable=False)


class SourceHealth(Base):
    __tablename__ = "source_health"
    source: Mapped[str] = mapped_column(Text, primary_key=True)
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_items: Mapped[int | None] = mapped_column(Integer)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_error: Mapped[str | None] = mapped_column(Text)
    month_calls: Mapped[int] = mapped_column(Integer, default=0)
    month_budget: Mapped[int | None] = mapped_column(Integer)
    month_key: Mapped[str | None] = mapped_column(Text)  # "2026-08" for resetting month_calls


class Subscription(Base):
    __tablename__ = "subscriptions"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(Text, nullable=False)  # topic|author|entity|source
    key: Mapped[str] = mapped_column(Text, nullable=False)
    config: Mapped[dict | None] = mapped_column(JSONB)
    weight: Mapped[float] = mapped_column(Float, default=1.0)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    __table_args__ = (UniqueConstraint("kind", "key", name="uq_subscriptions_kind_key"),)


class Entity(Base):
    __tablename__ = "entities"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    canonical_name: Mapped[str] = mapped_column(Text, unique=True, nullable=False)
    type: Mapped[str] = mapped_column(Text, nullable=False, default="project")
    anchors: Mapped[dict | None] = mapped_column(JSONB)
    first_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    watched: Mapped[bool] = mapped_column(Boolean, default=False)
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text, default="active")
    # agent-written "current state" card (docs/02 §5.6): {status, activity, trend, advice}
    brief: Mapped[dict | None] = mapped_column(JSONB)
    brief_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brief_model: Mapped[str | None] = mapped_column(Text)
    brief_source_count: Mapped[int | None] = mapped_column(Integer)   # timeline size when written


class EntityAlias(Base):
    __tablename__ = "entity_aliases"
    alias: Mapped[str] = mapped_column(Text, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))


class EntityTimeline(Base):
    __tablename__ = "entity_timeline"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    entity_id: Mapped[int] = mapped_column(ForeignKey("entities.id", ondelete="CASCADE"))
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"))
    event_type: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    note: Mapped[str | None] = mapped_column(Text)
    __table_args__ = (UniqueConstraint("entity_id", "item_id", name="uq_entity_timeline"),)


class Feedback(Base):
    __tablename__ = "feedback"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), index=True)
    action: Mapped[str] = mapped_column(Text, nullable=False)  # save|ignore|mute_source|click|expand|dig|read
    channel: Mapped[str | None] = mapped_column(Text)
    note: Mapped[str | None] = mapped_column(Text)
    ts: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class FeedbackFeatures(Base):
    __tablename__ = "feedback_features"
    feedback_id: Mapped[int] = mapped_column(
        ForeignKey("feedback.id", ondelete="CASCADE"), primary_key=True
    )
    ranker_version: Mapped[str | None] = mapped_column(Text)
    score: Mapped[float | None] = mapped_column(Float)
    score_breakdown: Mapped[dict | None] = mapped_column(JSONB)
    rank_in_digest: Mapped[int | None] = mapped_column(Integer)
    tags: Mapped[dict | None] = mapped_column(JSONB)
    sources: Mapped[list | None] = mapped_column(JSONB)


class Preference(Base):
    __tablename__ = "preferences"
    kind: Mapped[str] = mapped_column(Text, primary_key=True)  # tag|source|author|entity
    key: Mapped[str] = mapped_column(Text, primary_key=True)
    alpha: Mapped[float] = mapped_column(Float, default=1.0)
    beta: Mapped[float] = mapped_column(Float, default=1.0)
    muted_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AgentTask(Base):
    __tablename__ = "agent_tasks"
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    type: Mapped[str] = mapped_column(Text, nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False)
    status: Mapped[str] = mapped_column(Text, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    result: Mapped[dict | None] = mapped_column(JSONB)
    error: Mapped[str | None] = mapped_column(Text)
    model: Mapped[str | None] = mapped_column(Text)
    prompt_version: Mapped[str | None] = mapped_column(Text)
    tokens_in: Mapped[int | None] = mapped_column(Integer)
    tokens_out: Mapped[int | None] = mapped_column(Integer)
    cost_usd: Mapped[float | None] = mapped_column(Numeric(12, 6))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_agent_tasks_status_created", "status", "created_at"),)


class LlmUsage(Base):
    __tablename__ = "llm_usage"
    day: Mapped[date] = mapped_column(Date, primary_key=True)
    calls: Mapped[int] = mapped_column(Integer, default=0)
    tokens_in: Mapped[int] = mapped_column(BigInteger, default=0)
    tokens_out: Mapped[int] = mapped_column(BigInteger, default=0)
    cost_usd: Mapped[float] = mapped_column(Numeric(12, 6), default=0)


class Digest(Base):
    __tablename__ = "digests"
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    day: Mapped[date] = mapped_column(Date, nullable=False)
    kind: Mapped[str] = mapped_column(Text, default="daily")
    markdown: Mapped[str | None] = mapped_column(Text)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    stats: Mapped[dict | None] = mapped_column(JSONB)
    __table_args__ = (UniqueConstraint("day", "kind", name="uq_digests_day_kind"),)


class DigestItem(Base):
    __tablename__ = "digest_items"
    digest_id: Mapped[int] = mapped_column(ForeignKey("digests.id", ondelete="CASCADE"), primary_key=True)
    item_id: Mapped[int] = mapped_column(ForeignKey("items.id", ondelete="CASCADE"), primary_key=True)
    section: Mapped[str | None] = mapped_column(Text)  # top|folded|recall
    position: Mapped[int | None] = mapped_column(Integer)
