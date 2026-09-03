"""Canned Responses / Templates -- `canned_responses` table.

Revision ID: 0052_canned_responses
Revises: 0051_moderation_escalations
Create Date: 2026-09-03

See `app/models/canned_response.py`'s own module docstring for the full
Step 1/2 investigation and design. No new `ModerationAction`/enum
values needed -- template CRUD itself is administrative configuration,
not a moderation decision on a case, matching the SAME established
precedent `BlacklistedPattern` CRUD already set (create_pattern/
delete_pattern write no `ModerationLogEntry` either; only a real
content-rejection TRIGGER event does).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0052_canned_responses"
down_revision: str | None = "0051_moderation_escalations"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "canned_responses",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("title", sa.Text(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column(
            "applies_to",
            postgresql.ENUM(name="moderation_target_type", create_type=False),
            nullable=True,
        ),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("times_used", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
    )


def downgrade() -> None:
    op.drop_table("canned_responses")
