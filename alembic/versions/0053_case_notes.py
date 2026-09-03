"""Internal Notes on Cases -- `case_notes` table.

Revision ID: 0053_case_notes
Revises: 0052_canned_responses
Create Date: 2026-09-03

See `app/models/case_note.py`'s own module docstring for the full Step
1/2 investigation and design. No new `ModerationAction`/enum values --
same established precedent `CannedResponse` CRUD already set: a note is
shared internal context, not itself a moderation DECISION on a case, so
it isn't written to `ModerationLogEntry` either.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0053_case_notes"
down_revision: str | None = "0052_canned_responses"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "case_notes",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "item_type",
            postgresql.ENUM(name="moderation_target_type", create_type=False),
            nullable=False,
        ),
        sa.Column("item_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "author_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_case_notes_item", "case_notes", ["item_type", "item_id", "created_at"])


def downgrade() -> None:
    op.drop_index("ix_case_notes_item", table_name="case_notes")
    op.drop_table("case_notes")
