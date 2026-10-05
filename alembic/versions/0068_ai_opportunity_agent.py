"""APEX AI Opportunity Agent -- source reputation, source messages,
persistent pipeline jobs, dedup fingerprints, drafts, publications and
performance.

Revision ID: 0068_ai_opportunity_agent
Revises: 0067_onboarding_profile_fields
Create Date: 2026-10-05

Purely additive:
- `sources.trust_score` (FLOAT NOT NULL DEFAULT 0.5) and
  `sources.access_status` (VARCHAR(20) NOT NULL DEFAULT 'approved'):
  every existing row gets the neutral/approved value; no data rewrite.
- New tables only: source_messages, opportunity_source_messages,
  opportunity_fingerprints, agent_opportunity_facts, pipeline_jobs,
  pipeline_job_events, agent_post_drafts, publications, post_performance.
- `opportunities` itself is NOT altered.

Downgrade drops exactly what upgrade created (destroys agent data -- run
only deliberately).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0068_ai_opportunity_agent"
down_revision = "0067_onboarding_profile_fields"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_JSONB = postgresql.JSONB(astext_type=sa.Text())
_UUID = postgresql.UUID(as_uuid=True)


def _timestamps() -> list[sa.Column]:
    return [
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    ]


def upgrade() -> None:
    op.add_column("sources", sa.Column("trust_score", sa.Float(), server_default="0.5", nullable=False))
    op.add_column(
        "sources", sa.Column("access_status", sa.String(length=20), server_default="approved", nullable=False)
    )
    op.create_check_constraint("ck_sources_trust_score_range", "sources", "trust_score >= 0 AND trust_score <= 1")

    op.create_table(
        "source_messages",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("source_id", _UUID, sa.ForeignKey("sources.id", ondelete="RESTRICT"), nullable=False),
        sa.Column("source_type", sa.String(length=20), nullable=False),
        sa.Column("external_message_id", sa.String(length=255), nullable=True),
        sa.Column("sender_id", sa.String(length=255), nullable=True),
        sa.Column("sender_name", sa.String(length=255), nullable=True),
        sa.Column("group_id", sa.String(length=255), nullable=True),
        sa.Column("group_name", sa.String(length=255), nullable=True),
        sa.Column("raw_text", sa.Text(), nullable=False, server_default=""),
        sa.Column("links", _JSONB, nullable=True),
        sa.Column("attachment_metadata", _JSONB, nullable=True),
        sa.Column("normalized_content", sa.Text(), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processing_status", sa.String(length=30), nullable=False, server_default="received"),
        sa.Column("extraction", _JSONB, nullable=True),
        sa.Column("dedup_outcome", sa.String(length=30), nullable=True),
        sa.Column("dedup_details", _JSONB, nullable=True),
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("source_id", "external_message_id", name="uq_source_messages_source_external_id"),
    )
    op.create_index("ix_source_messages_source_id", "source_messages", ["source_id"])
    op.create_index("ix_source_messages_content_hash", "source_messages", ["content_hash"])
    op.create_index("ix_source_messages_status", "source_messages", ["processing_status"])
    op.create_index("ix_source_messages_opportunity_id", "source_messages", ["opportunity_id"])

    op.create_table(
        "opportunity_source_messages",
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True),
        sa.Column(
            "source_message_id", _UUID, sa.ForeignKey("source_messages.id", ondelete="CASCADE"), primary_key=True
        ),
        sa.Column("relation", sa.String(length=20), nullable=False, server_default="origin"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )

    op.create_table(
        "opportunity_fingerprints",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("kind", sa.String(length=30), nullable=False),
        sa.Column("value_hash", sa.String(length=64), nullable=False),
        sa.Column("value", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.UniqueConstraint("opportunity_id", "kind", "value_hash", name="uq_opportunity_fingerprints_opp_kind_hash"),
    )
    op.create_index("ix_opportunity_fingerprints_opportunity_id", "opportunity_fingerprints", ["opportunity_id"])
    op.create_index("ix_opportunity_fingerprints_kind_hash", "opportunity_fingerprints", ["kind", "value_hash"])

    op.create_table(
        "agent_opportunity_facts",
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("organization", sa.String(length=500), nullable=True),
        sa.Column("application_url", sa.String(length=2048), nullable=True),
        sa.Column("official_url", sa.String(length=2048), nullable=True),
        sa.Column("facts", _JSONB, nullable=False),
        sa.Column("verification_level", sa.String(length=30), nullable=True),
        sa.Column("verification_report", _JSONB, nullable=True),
        *_timestamps(),
    )

    op.create_table(
        "pipeline_jobs",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("job_type", sa.String(length=30), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("outcome", sa.String(length=40), nullable=True),
        sa.Column("priority", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "source_message_id", _UUID, sa.ForeignKey("source_messages.id", ondelete="CASCADE"), nullable=True
        ),
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="SET NULL"), nullable=True),
        sa.Column("draft_id", _UUID, nullable=True),  # FK added after agent_post_drafts exists
        sa.Column("payload", _JSONB, nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer(), nullable=False, server_default="5"),
        sa.Column("next_retry_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("last_error_code", sa.String(length=80), nullable=True),
        sa.Column("last_error_category", sa.String(length=30), nullable=True),
        sa.Column("locked_by", sa.String(length=120), nullable=True),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.CheckConstraint("attempts >= 0 AND attempts <= max_attempts", name="ck_pipeline_jobs_attempts_bounded"),
    )
    op.create_index("ix_pipeline_jobs_status_next_retry", "pipeline_jobs", ["status", "next_retry_at"])
    op.create_index("ix_pipeline_jobs_locked_until", "pipeline_jobs", ["locked_until"])
    op.create_index("ix_pipeline_jobs_source_message_id", "pipeline_jobs", ["source_message_id"])
    op.create_index("ix_pipeline_jobs_opportunity_id", "pipeline_jobs", ["opportunity_id"])

    op.create_table(
        "pipeline_job_events",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("job_id", _UUID, sa.ForeignKey("pipeline_jobs.id", ondelete="CASCADE"), nullable=False),
        sa.Column("from_status", sa.String(length=30), nullable=True),
        sa.Column("to_status", sa.String(length=30), nullable=False),
        sa.Column("stage", sa.String(length=40), nullable=True),
        sa.Column("detail", _JSONB, nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_pipeline_job_events_job_id", "pipeline_job_events", ["job_id"])

    op.create_table(
        "agent_post_drafts",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("job_id", _UUID, sa.ForeignKey("pipeline_jobs.id", ondelete="SET NULL"), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=10), nullable=False, server_default="ar"),
        sa.Column("writer_mode", sa.String(length=20), nullable=False),
        sa.Column("style_example_ids", _JSONB, nullable=True),
        sa.Column("fact_guard", _JSONB, nullable=True),
        sa.Column("review_decision", sa.String(length=20), nullable=True),
        sa.Column("review_reasons", _JSONB, nullable=True),
        sa.Column("status", sa.String(length=30), nullable=False, server_default="draft"),
        sa.Column("embedding", _JSONB, nullable=True),
        sa.Column("decided_by", _UUID, nullable=True),
        sa.Column("decided_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("decision_notes", sa.Text(), nullable=True),
        *_timestamps(),
    )
    op.create_index("ix_agent_post_drafts_opportunity_id", "agent_post_drafts", ["opportunity_id"])
    op.create_index("ix_agent_post_drafts_status", "agent_post_drafts", ["status"])
    op.create_foreign_key(
        "fk_pipeline_jobs_draft_id", "pipeline_jobs", "agent_post_drafts", ["draft_id"], ["id"], ondelete="SET NULL"
    )

    op.create_table(
        "publications",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("idempotency_key", sa.String(length=64), nullable=False),
        sa.Column("draft_id", _UUID, sa.ForeignKey("agent_post_drafts.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=30), nullable=False),
        sa.Column("target", sa.String(length=255), nullable=False, server_default="default"),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("external_id", sa.String(length=255), nullable=True),
        sa.Column("external_url", sa.String(length=2048), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        *_timestamps(),
        sa.UniqueConstraint("idempotency_key", name="uq_publications_idempotency_key"),
    )
    op.create_index("ix_publications_draft_id", "publications", ["draft_id"])
    op.create_index("ix_publications_opportunity_id", "publications", ["opportunity_id"])

    op.create_table(
        "post_performance",
        sa.Column("id", _UUID, primary_key=True),
        sa.Column("publication_id", _UUID, sa.ForeignKey("publications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("opportunity_id", _UUID, sa.ForeignKey("opportunities.id", ondelete="CASCADE"), nullable=False),
        sa.Column("platform", sa.String(length=30), nullable=False),
        sa.Column("views", sa.Integer(), nullable=True),
        sa.Column("reactions", sa.Integer(), nullable=True),
        sa.Column("comments", sa.Integer(), nullable=True),
        sa.Column("shares", sa.Integer(), nullable=True),
        sa.Column("clicks", sa.Integer(), nullable=True),
        sa.Column("applications", sa.Integer(), nullable=True),
        sa.Column("engagement_score", sa.Float(), nullable=True),
        sa.Column("measurement_source", sa.String(length=30), nullable=False, server_default="manual"),
        sa.Column("measured_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
    )
    op.create_index("ix_post_performance_publication_id", "post_performance", ["publication_id"])
    op.create_index("ix_post_performance_opportunity_id", "post_performance", ["opportunity_id"])


def downgrade() -> None:
    op.drop_index("ix_post_performance_opportunity_id", table_name="post_performance")
    op.drop_index("ix_post_performance_publication_id", table_name="post_performance")
    op.drop_table("post_performance")
    op.drop_index("ix_publications_opportunity_id", table_name="publications")
    op.drop_index("ix_publications_draft_id", table_name="publications")
    op.drop_table("publications")
    op.drop_constraint("fk_pipeline_jobs_draft_id", "pipeline_jobs", type_="foreignkey")
    op.drop_index("ix_agent_post_drafts_status", table_name="agent_post_drafts")
    op.drop_index("ix_agent_post_drafts_opportunity_id", table_name="agent_post_drafts")
    op.drop_table("agent_post_drafts")
    op.drop_index("ix_pipeline_job_events_job_id", table_name="pipeline_job_events")
    op.drop_table("pipeline_job_events")
    op.drop_index("ix_pipeline_jobs_opportunity_id", table_name="pipeline_jobs")
    op.drop_index("ix_pipeline_jobs_source_message_id", table_name="pipeline_jobs")
    op.drop_index("ix_pipeline_jobs_locked_until", table_name="pipeline_jobs")
    op.drop_index("ix_pipeline_jobs_status_next_retry", table_name="pipeline_jobs")
    op.drop_table("pipeline_jobs")
    op.drop_table("agent_opportunity_facts")
    op.drop_index("ix_opportunity_fingerprints_kind_hash", table_name="opportunity_fingerprints")
    op.drop_index("ix_opportunity_fingerprints_opportunity_id", table_name="opportunity_fingerprints")
    op.drop_table("opportunity_fingerprints")
    op.drop_table("opportunity_source_messages")
    op.drop_index("ix_source_messages_opportunity_id", table_name="source_messages")
    op.drop_index("ix_source_messages_status", table_name="source_messages")
    op.drop_index("ix_source_messages_content_hash", table_name="source_messages")
    op.drop_index("ix_source_messages_source_id", table_name="source_messages")
    op.drop_table("source_messages")
    op.drop_constraint("ck_sources_trust_score_range", "sources", type_="check")
    op.drop_column("sources", "access_status")
    op.drop_column("sources", "trust_score")
