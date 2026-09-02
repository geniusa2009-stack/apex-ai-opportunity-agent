"""Volunteer Coverage / Shift Schedule -- `shift_assignments`.

Revision ID: 0049_shift_assignments
Revises: 0048_onboarding_checklist
Create Date: 2026-09-02

See `app/models/shift.py`'s own module docstring for the full Step 1/2
investigation and design reasoning: no shift/coverage/schedule model
existed anywhere in this codebase before this feature.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0049_shift_assignments"
down_revision: str | None = "0048_onboarding_checklist"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "shift_assignments",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "volunteer_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("start_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("end_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("notes", sa.Text(), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_shift_assignments_volunteer_id", "shift_assignments", ["volunteer_id"])
    op.create_index("ix_shift_assignments_start_at", "shift_assignments", ["start_at"])


def downgrade() -> None:
    op.drop_index("ix_shift_assignments_start_at", table_name="shift_assignments")
    op.drop_index("ix_shift_assignments_volunteer_id", table_name="shift_assignments")
    op.drop_table("shift_assignments")
