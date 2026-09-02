"""New-Volunteer Onboarding Checklist -- `users.role_granted_at` +
`onboarding_checklist_progress`.

Revision ID: 0048_onboarding_checklist
Revises: 0047_guild_content_reports
Create Date: 2026-09-02

See `app/models/onboarding.py`'s own module docstring for the full
Step 1/2 investigation: `PATCH /admin/users/{user_id}/role` was the
one real hook point for "this person is now a new volunteer," but
nothing ever recorded WHEN a tier was granted, and no per-volunteer
onboarding state existed anywhere in this codebase.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0048_onboarding_checklist"
down_revision: str | None = "0047_guild_content_reports"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("role_granted_at", sa.DateTime(timezone=True), nullable=True))

    op.create_table(
        "onboarding_checklist_progress",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "user_id",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("step_key", sa.String(50), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("user_id", "step_key", name="uq_onboarding_progress_user_step"),
    )
    op.create_index("ix_onboarding_progress_user_id", "onboarding_checklist_progress", ["user_id"])


def downgrade() -> None:
    op.drop_index("ix_onboarding_progress_user_id", table_name="onboarding_checklist_progress")
    op.drop_table("onboarding_checklist_progress")
    op.drop_column("users", "role_granted_at")
