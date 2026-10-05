"""Persistent state for the APEX AI Opportunity Agent.

Relationship model (spec section 8)::

    Source 1--* SourceMessage *--* Opportunity   (via opportunity_source_messages)
                     |                  |
                     |                  +-- Claim/Evidence (existing D6 tables)
                     |                  +-- AgentOpportunityFacts (1:1, rich facts)
                     |                  +-- OpportunityFingerprint (dedup keys)
                     +-- PipelineJob ---+-- AgentPostDraft --* Publication --* PostPerformance

Portability: generic `Uuid`/`JSON` types (JSONB on Postgres) so the same
models run on the production Postgres and on the in-memory SQLite used by
the unit tests. Nothing here changes the existing `opportunities` table,
so none of the ~30 raw-SQL test fixtures for that table need to change.
"""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from sqlalchemy import (
    JSON,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.agent_enums import (
    DraftStatus,
    PipelineJobStatus,
    PipelineJobType,
    PublicationStatus,
    SourceMessageStatus,
)
from app.models.mixins import TimestampMixin

# JSONB on Postgres, plain JSON elsewhere (SQLite in tests).
JSONType = JSON().with_variant(JSONB(), "postgresql")


class SourceMessage(Base, TimestampMixin):
    """One raw incoming item (a WhatsApp message, a volunteer submission, a
    Telegram post, an RSS entry...). Never treated as an opportunity by
    itself -- extraction decides that."""

    __tablename__ = "source_messages"
    __table_args__ = (
        # Same platform message ingested twice -> rejected at the DB level.
        UniqueConstraint("source_id", "external_message_id", name="uq_source_messages_source_external_id"),
        Index("ix_source_messages_content_hash", "content_hash"),
        Index("ix_source_messages_status", "processing_status"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    source_type: Mapped[str] = mapped_column(String(20), nullable=False)
    external_message_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    sender_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    group_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    links: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    attachment_metadata: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType, nullable=True)
    normalized_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    received_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    processing_status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=SourceMessageStatus.RECEIVED.value
    )
    # Validated `ExtractedOpportunity` (model_dump) once extraction succeeded.
    extraction: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    dedup_outcome: Mapped[str | None] = mapped_column(String(30), nullable=True)
    dedup_details: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True, index=True
    )


class OpportunitySourceMessage(Base):
    """Many-to-many: one opportunity can be reported by many messages."""

    __tablename__ = "opportunity_source_messages"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True
    )
    source_message_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("source_messages.id", ondelete="CASCADE"), primary_key=True
    )
    relation: Mapped[str] = mapped_column(String(20), nullable=False, default="origin")  # origin|duplicate|update
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class OpportunityFingerprint(Base):
    """Deterministic dedup keys for an opportunity (exact layers 1-4).

    `value_hash` = sha256(normalized key) keeps the index small and
    fixed-width; `value` keeps the readable key for audits."""

    __tablename__ = "opportunity_fingerprints"
    __table_args__ = (
        UniqueConstraint("opportunity_id", "kind", "value_hash", name="uq_opportunity_fingerprints_opp_kind_hash"),
        Index("ix_opportunity_fingerprints_kind_hash", "kind", "value_hash"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    kind: Mapped[str] = mapped_column(String(30), nullable=False)
    value_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    value: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentOpportunityFacts(Base, TimestampMixin):
    """Rich, structured facts the agent knows about an opportunity -- the
    fields the legacy `opportunities` row has no column for (organization,
    application/official URL, dates, documents, contact) plus the latest
    verification report. 1:1 with `opportunities`."""

    __tablename__ = "agent_opportunity_facts"

    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True
    )
    organization: Mapped[str | None] = mapped_column(String(500), nullable=True)
    application_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    official_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    facts: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    verification_level: Mapped[str | None] = mapped_column(String(30), nullable=True)
    verification_report: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)


class PipelineJob(Base, TimestampMixin):
    """Durable unit of pipeline work. All state lives here, never only in
    process memory or Celery: a crashed worker's job is reclaimed once its
    lease (`locked_until`) expires and resumes from `status`."""

    __tablename__ = "pipeline_jobs"
    __table_args__ = (
        Index("ix_pipeline_jobs_status_next_retry", "status", "next_retry_at"),
        Index("ix_pipeline_jobs_locked_until", "locked_until"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_type: Mapped[str] = mapped_column(String(30), nullable=False, default=PipelineJobType.PROCESS_MESSAGE.value)
    correlation_id: Mapped[str] = mapped_column(String(64), nullable=False, default=lambda: uuid.uuid4().hex)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=PipelineJobStatus.QUEUED.value)
    outcome: Mapped[str | None] = mapped_column(String(40), nullable=True)
    priority: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    source_message_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("source_messages.id", ondelete="CASCADE"), nullable=True, index=True
    )
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True, index=True
    )
    draft_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("agent_post_drafts.id", ondelete="SET NULL", use_alter=True), nullable=True
    )
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)

    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    next_retry_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_error_code: Mapped[str | None] = mapped_column(String(80), nullable=True)
    last_error_category: Mapped[str | None] = mapped_column(String(30), nullable=True)

    locked_by: Mapped[str | None] = mapped_column(String(120), nullable=True)
    locked_until: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    started_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PipelineJobEvent(Base):
    """Append-only audit trail of every job transition -- answers "what
    happened to this message, and why" without grepping logs."""

    __tablename__ = "pipeline_job_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("pipeline_jobs.id", ondelete="CASCADE"), nullable=False, index=True
    )
    from_status: Mapped[str | None] = mapped_column(String(30), nullable=True)
    to_status: Mapped[str] = mapped_column(String(30), nullable=False)
    stage: Mapped[str | None] = mapped_column(String(40), nullable=True)
    detail: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    duration_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


class AgentPostDraft(Base, TimestampMixin):
    """One APEX post version produced by the Writer and judged by the
    Reviewer. Only a human moves it to APPROVED."""

    __tablename__ = "agent_post_drafts"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    job_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("pipeline_jobs.id", ondelete="SET NULL"), nullable=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    language: Mapped[str] = mapped_column(String(10), nullable=False, default="ar")
    writer_mode: Mapped[str] = mapped_column(String(20), nullable=False)
    style_example_ids: Mapped[list[str] | None] = mapped_column(JSONType, nullable=True)
    fact_guard: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    review_decision: Mapped[str | None] = mapped_column(String(20), nullable=True)
    review_reasons: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONType, nullable=True)
    status: Mapped[str] = mapped_column(String(30), nullable=False, default=DraftStatus.DRAFT.value, index=True)
    # Lexical/semantic vector for historical RAG. Stored as JSON (not
    # pgvector) so its size is independent of `embedding_dimension`; RAG
    # ranks a bounded recent window in Python -- see app/agent/rag.py.
    embedding: Mapped[list[float] | None] = mapped_column(JSONType, nullable=True)
    decided_by: Mapped[uuid.UUID | None] = mapped_column(Uuid, nullable=True)
    decided_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decision_notes: Mapped[str | None] = mapped_column(Text, nullable=True)


class Publication(Base, TimestampMixin):
    """One attempt-tracked publication of a draft to one platform/target.

    `idempotency_key` is UNIQUE: retries, restarts and duplicate approval
    clicks all resolve to the same row, so a post is sent at most once."""

    __tablename__ = "publications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    draft_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("agent_post_drafts.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    target: Mapped[str] = mapped_column(String(255), nullable=False, default="default")
    status: Mapped[str] = mapped_column(String(20), nullable=False, default=PublicationStatus.PENDING.value)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    external_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    published_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class PostPerformance(Base):
    """A measurement of how a publication performed. Every metric is
    nullable: a metric that was not measured is NULL, never 0 or a guess."""

    __tablename__ = "post_performance"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    publication_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("publications.id", ondelete="CASCADE"), nullable=False, index=True
    )
    opportunity_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False, index=True
    )
    platform: Mapped[str] = mapped_column(String(30), nullable=False)
    views: Mapped[int | None] = mapped_column(Integer, nullable=True)
    reactions: Mapped[int | None] = mapped_column(Integer, nullable=True)
    comments: Mapped[int | None] = mapped_column(Integer, nullable=True)
    shares: Mapped[int | None] = mapped_column(Integer, nullable=True)
    clicks: Mapped[int | None] = mapped_column(Integer, nullable=True)
    applications: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Derived only when the inputs exist (see app/agent/performance.py).
    engagement_score: Mapped[float | None] = mapped_column(Float, nullable=True)
    measurement_source: Mapped[str] = mapped_column(String(30), nullable=False, default="manual")
    measured_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)
