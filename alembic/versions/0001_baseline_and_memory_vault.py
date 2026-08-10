"""Baseline schema + Enterprise-Grade AI Memory & Cloud Vault System.

Revision ID: 0001_baseline_and_memory_vault
Revises:
Create Date: 2026-07-16

IMPORTANT CONTEXT (disclosed judgment call -- see the README section for
this step, "On the migration itself"): `alembic/versions/` was completely
empty before this migration -- NO migration has ever been generated for
this project, for ANY of its existing tables, despite six models
(User, Source, Opportunity, UserBookmark, Alumnus, AgentAlert) already
being used against a real Supabase Postgres database since as far back as
Phase 1.2. Nothing in this codebase calls `Base.metadata.create_all()`
either (confirmed via a full-repo check of app/main.py and every
app/workers/*.py before writing this) -- so Alembic is genuinely this
project's only schema-provisioning mechanism, and until this file existed,
running `alembic upgrade head` against a fresh database would have created
NOTHING at all.

This step's ask asked for one thing specifically: "include a raw SQL
execution in migrations: `CREATE EXTENSION IF NOT EXISTS vector;`" --
implying an existing migration to add that statement to. Since none
exists, the honest choice is a single genuine BASELINE migration: this
file creates the two new tables the ask asks for (`user_history`,
`student_memories`) AND retroactively captures the six pre-existing
tables' current shape, in the dependency order their foreign keys
require. This was NOT built via `alembic revision --autogenerate`
(the sandbox this was built in has no root access to install a real
Postgres server -- confirmed and disclosed in this step's README) --
it's hand-written directly from each model file in app/models/, cross-
checked column-by-column against them. If a live Supabase database this
migration is run against already has some of these tables from manual
provisioning, `alembic stamp head` (not `upgrade head`) is the correct
command to mark this baseline as already-applied without re-running its
DDL -- also covered in the README's manual testing notes for this step.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision: str = "0001_baseline_and_memory_vault"
down_revision: str | None = None
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Must match app.models.opportunity.EMBEDDING_DIM (OpenAI text-embedding-3-small).
_OPPORTUNITY_EMBEDDING_DIM = 1536
# Must match app.models.student_memory.MEMORY_EMBEDDING_DIM (Gemini text-embedding-004).
_MEMORY_EMBEDDING_DIM = 768


def upgrade() -> None:
    # Required before any `Vector(...)` column can be created -- per this
    # step's explicit instruction. Must run before `opportunities` and
    # `student_memories` below, both of which have a Vector column.
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")

    # --- users ---------------------------------------------------------
    op.create_table(
        "users",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column("hashed_password", sa.String(255), nullable=True),
        sa.Column("full_name", sa.String(255), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("is_superuser", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("phone", sa.String(50), nullable=True),
        sa.Column("degree_level", sa.String(100), nullable=True),
        sa.Column("major", sa.String(255), nullable=True),
        sa.Column("gpa", sa.Float(), nullable=True),
        sa.Column("graduation_year", sa.Integer(), nullable=True),
        sa.Column("skills", postgresql.ARRAY(sa.String(100)), nullable=True),
        sa.Column("target_country", sa.String(120), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # --- sources ---------------------------------------------------------
    op.create_table(
        "sources",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("name", sa.String(255), nullable=False),
        sa.Column("base_url", sa.String(2048), nullable=False),
        sa.Column("active_status", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_sources_active_status", "sources", ["active_status"])

    # --- opportunities ---------------------------------------------------
    op.create_table(
        "opportunities",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("description", sa.Text(), nullable=True),
        sa.Column("target_url", sa.String(2048), nullable=False),
        sa.Column(
            "source_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("sources.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "type",
            sa.Enum("scholarship", "internship", "volunteer", "course", name="opportunity_type"),
            nullable=False,
        ),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=True),
        sa.Column("eligibility_criteria", sa.Text(), nullable=True),
        sa.Column("funding_details", sa.Text(), nullable=True),
        sa.Column("location", sa.String(255), nullable=True),
        sa.Column("country", sa.String(120), nullable=True),
        sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("ai_confidence_score", sa.Float(), nullable=True),
        sa.Column("raw_extracted_data", postgresql.JSONB(), nullable=True),
        sa.Column("embedding", Vector(_OPPORTUNITY_EMBEDDING_DIM), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_opportunities_source_id", "opportunities", ["source_id"])
    op.create_index("ix_opportunities_type", "opportunities", ["type"])
    op.create_index("ix_opportunities_country", "opportunities", ["country"])
    op.create_index("ix_opportunities_is_verified", "opportunities", ["is_verified"])
    op.create_index("ix_opportunities_type_deadline", "opportunities", ["type", "deadline"])

    # --- user_bookmarks ----------------------------------------------------
    op.create_table(
        "user_bookmarks",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.Enum("saved", "applied", name="bookmark_status"),
            nullable=False,
            server_default="saved",
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("user_id", "opportunity_id", name="uq_user_opportunity_bookmark"),
    )
    op.create_index("ix_user_bookmarks_user_id", "user_bookmarks", ["user_id"])
    op.create_index("ix_user_bookmarks_opportunity_id", "user_bookmarks", ["opportunity_id"])

    # --- alumni -----------------------------------------------------------
    op.create_table(
        "alumni",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("full_name", sa.String(255), nullable=False),
        sa.Column("university", sa.String(255), nullable=True),
        sa.Column("major", sa.String(255), nullable=True),
        sa.Column("country", sa.String(120), nullable=True),
        sa.Column("degree_level", sa.String(100), nullable=True),
        sa.Column("current_position", sa.String(500), nullable=True),
        sa.Column("bio", sa.Text(), nullable=True),
        sa.Column("linkedin_url", sa.String(2048), nullable=True),
        sa.Column("email", sa.String(255), nullable=True),
        sa.Column(
            "matched_opportunity_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("opportunities.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_alumni_matched_opportunity_id", "alumni", ["matched_opportunity_id"])

    # --- agent_alerts -------------------------------------------------------
    op.create_table(
        "agent_alerts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("search_query", sa.Text(), nullable=False),
        sa.Column("report_text", sa.Text(), nullable=False),
        sa.Column("matches", postgresql.JSONB(), nullable=False, server_default="[]"),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_agent_alerts_user_id", "agent_alerts", ["user_id"])

    # --- user_history (NEW -- this step, Layer 1: metadata) ---------------
    op.create_table(
        "user_history",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("feature_key", sa.String(100), nullable=False),
        sa.Column("title", sa.String(500), nullable=False),
        sa.Column("storage_path", sa.String(500), nullable=False),
        sa.Column("is_pinned", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_index("ix_user_history_user_id", "user_history", ["user_id"])
    op.create_index("ix_user_history_feature_key", "user_history", ["feature_key"])

    # --- student_memories (NEW -- this step, Layer 3: semantic memory) -----
    op.create_table(
        "student_memories",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "artifact_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("user_history.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("memory_text", sa.Text(), nullable=False),
        sa.Column("embedding", Vector(_MEMORY_EMBEDDING_DIM), nullable=False),
    )
    op.create_index("ix_student_memories_user_id", "student_memories", ["user_id"])
    op.create_index("ix_student_memories_artifact_id", "student_memories", ["artifact_id"])

    # No ANN index (ivfflat/hnsw) on either Vector column yet -- same
    # "deferred pending real data volume" posture already disclosed for
    # `opportunities.embedding` (app/models/opportunity.py). An exact
    # nearest-neighbor scan is correct at today's data volume either way.


def downgrade() -> None:
    # Reverse dependency order.
    op.drop_table("student_memories")
    op.drop_table("user_history")
    op.drop_table("agent_alerts")
    op.drop_table("alumni")
    op.drop_table("user_bookmarks")
    op.drop_table("opportunities")
    op.drop_table("sources")
    op.drop_table("users")

    bind = op.get_bind()
    sa.Enum(name="opportunity_type").drop(bind, checkfirst=True)
    sa.Enum(name="bookmark_status").drop(bind, checkfirst=True)

    # Deliberately NOT dropping the `vector` extension here -- doing so on
    # downgrade would be destructive to anything else in the same
    # database that might depend on it, and extensions are cheap/harmless
    # to leave installed. Same "don't reverse a shared, low-cost
    # infrastructure change just because this migration added it" posture
    # you'd apply to e.g. `CREATE EXTENSION IF NOT EXISTS pgcrypto` in any
    # other project.
