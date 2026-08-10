"""Hyper-Personalized Platform step: polymorphic bookmarks + advanced
integrated schemas.

Revision ID: 0027_hyper_personalized_platform
Revises: 0026_academic_level
Create Date: 2026-07-18

Two disclosed pieces, both covered at length in `app/models/bookmark.py`
and `app/models/features.py`'s own docstrings -- summarized here:

1. `user_bookmarks` polymorphism -- `opportunity_id` becomes nullable
   (was NOT NULL), a new nullable `course_id` FK to `courses.id` is
   added, a second parallel `UniqueConstraint` (`user_id`, `course_id`)
   is added alongside the pre-existing `(user_id, opportunity_id)` one,
   and a `CheckConstraint` (`ck_bookmark_exactly_one_target`) enforces
   exactly one of the two is ever set on a given row.

   DISCLOSED DOWNGRADE CAVEAT: `downgrade()` restores `opportunity_id`
   to NOT NULL. This is only safe if no course-typed bookmark rows
   (`opportunity_id IS NULL`) exist at downgrade time -- true for every
   environment this migration has actually been tested against (no
   course-bookmarking endpoint exists yet as of this step, see
   `app/models/bookmark.py`'s own disclosed scope boundary), but a real
   downgrade against a database that DOES have course-only bookmark rows
   would fail the `NOT NULL` constraint and needs those rows deleted or
   backfilled with a real `opportunity_id` first. Same class of caveat
   every structural downgrade in this project carries (data, unlike
   schema, is never automatically reconciled by these scripts).

2. Three brand-new tables (`certificates`, `lesson_study_aids`,
   `mentorship_relations`), fully specified by `app/models/features.py` --
   see that module's own docstring for the disclosed reasoning behind
   each one's exact column set, constraints, and the deliberate absence
   of ORM `relationship()` back-references (which don't affect the SQL
   schema here regardless).

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0027_hyper_personalized_platform"
down_revision: str | None = "0026_academic_level"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    # --- 1. `user_bookmarks` polymorphism ---
    op.alter_column("user_bookmarks", "opportunity_id", existing_type=postgresql.UUID(as_uuid=True), nullable=True)

    op.add_column(
        "user_bookmarks",
        sa.Column(
            "course_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=True,
        ),
    )
    op.create_index("ix_user_bookmarks_course_id", "user_bookmarks", ["course_id"], unique=False)

    op.create_unique_constraint(
        "uq_user_course_bookmark", "user_bookmarks", ["user_id", "course_id"]
    )
    op.create_check_constraint(
        "ck_bookmark_exactly_one_target",
        "user_bookmarks",
        "(course_id IS NOT NULL AND opportunity_id IS NULL) "
        "OR (course_id IS NULL AND opportunity_id IS NOT NULL)",
    )

    # --- 2a. `certificates` ---
    op.create_table(
        "certificates",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "course_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("courses.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "issued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("certificate_url", sa.String(length=2048), nullable=True),
        sa.UniqueConstraint("user_id", "course_id", name="uq_certificate_user_course"),
    )
    op.create_index("ix_certificates_user_id", "certificates", ["user_id"], unique=False)
    op.create_index("ix_certificates_course_id", "certificates", ["course_id"], unique=False)

    # --- 2b. `lesson_study_aids` ---
    op.create_table(
        "lesson_study_aids",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "lesson_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("lessons.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column(
            "student_video_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("student_videos.id", ondelete="CASCADE"),
            nullable=True,
            unique=True,
        ),
        sa.Column("summary_text", sa.Text(), nullable=True),
        sa.Column("flashcards", postgresql.JSONB(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "(lesson_id IS NOT NULL AND student_video_id IS NULL) "
            "OR (lesson_id IS NULL AND student_video_id IS NOT NULL)",
            name="ck_study_aid_exactly_one_target",
        ),
    )
    # `unique=True` on the two columns above already gives each its own
    # unique index (Postgres creates one implicitly to enforce it) -- no
    # separate `op.create_index` call needed for either, unlike
    # `certificates`/`mentorship_relations`'s plain (non-unique) FK
    # indexes above/below, which DO need an explicit index.

    # --- 2c. `mentorship_relations` ---
    op.create_table(
        "mentorship_relations",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "mentor_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "mentee_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column(
            "requested_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("responded_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("mentor_id", "mentee_id", name="uq_mentorship_pair"),
        sa.CheckConstraint("mentor_id != mentee_id", name="ck_mentorship_not_self"),
    )
    op.create_index("ix_mentorship_relations_mentor_id", "mentorship_relations", ["mentor_id"], unique=False)
    op.create_index("ix_mentorship_relations_mentee_id", "mentorship_relations", ["mentee_id"], unique=False)


def downgrade() -> None:
    op.drop_index("ix_mentorship_relations_mentee_id", table_name="mentorship_relations")
    op.drop_index("ix_mentorship_relations_mentor_id", table_name="mentorship_relations")
    op.drop_table("mentorship_relations")

    op.drop_table("lesson_study_aids")

    op.drop_index("ix_certificates_course_id", table_name="certificates")
    op.drop_index("ix_certificates_user_id", table_name="certificates")
    op.drop_table("certificates")

    op.drop_constraint("ck_bookmark_exactly_one_target", "user_bookmarks", type_="check")
    op.drop_constraint("uq_user_course_bookmark", "user_bookmarks", type_="unique")
    op.drop_index("ix_user_bookmarks_course_id", table_name="user_bookmarks")
    op.drop_column("user_bookmarks", "course_id")
    # See module docstring's "DISCLOSED DOWNGRADE CAVEAT" -- only safe if
    # no course-only bookmark rows exist.
    op.alter_column("user_bookmarks", "opportunity_id", existing_type=postgresql.UUID(as_uuid=True), nullable=False)
