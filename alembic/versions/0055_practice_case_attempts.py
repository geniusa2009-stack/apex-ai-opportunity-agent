"""Training / Practice Mode for New Volunteers -- `practice_case_attempts`
table.

Revision ID: 0055_practice_case_attempts
Revises: 0054_dangerous_action_confirmations
Create Date: 2026-09-03

See `app/models/practice_case.py`'s own module docstring for the full
Step 1 investigation and Step 2 scope decisions, especially STEP 3.1
(why this is the ONLY new table this feature adds -- no fake rows in
any real item-type table, no `is_practice` column bolted onto
`student_videos`/`chat_message_reports`/`guild_content_reports`). No
new `ModerationAction`/`moderation_target_type` enum values -- a
practice attempt is, by design, never written to `ModerationLogEntry`
at all (that table's real, established meaning is a real trust-and-
safety action against a real target; a practice attempt is neither).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0055_practice_case_attempts"
down_revision: str | None = "0054_dangerous_action_confirmations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "practice_case_attempts",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("case_key", sa.String(length=50), nullable=False),
        sa.Column("action_taken", sa.String(length=30), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column("acted_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "case_key", name="uq_practice_attempt_user_case"),
    )
    op.create_index("ix_practice_attempts_user_id", "practice_case_attempts", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_practice_attempts_user_id", table_name="practice_case_attempts")
    op.drop_table("practice_case_attempts")
