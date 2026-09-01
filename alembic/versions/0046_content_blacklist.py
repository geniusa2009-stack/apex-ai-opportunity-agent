"""General Content Blacklist / Pattern Auto-Trigger -- new
`blacklisted_patterns` table, plus one new `ModerationAction` enum value
(`blacklist_triggered`).

Revision ID: 0046_content_blacklist
Revises: 0045_activity_log_export
Create Date: 2026-09-01

See `app/models/blacklist.py`'s own module docstring for the full Step 1
"no blacklist storage existed anywhere" finding and Step 2 matching-
mechanism/regex-opt-in design, and `app/services/blacklist_service.py`'s
own module docstring for the full "reject, don't ban" trigger-response
reasoning this new `ModerationAction` value backs. No new
`ModerationTargetType` value is needed -- every trigger targets the
submitting user directly (`target_type=USER`), same convention
`CHAT_AUTO_BAN`/`CHAT_UNBAN`/`CHAT_MANUAL_BAN` already established.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0046_content_blacklist"
down_revision: str | None = "0045_activity_log_export"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'blacklist_triggered'")

    op.create_table(
        "blacklisted_patterns",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("pattern", sa.String(length=200), nullable=False),
        sa.Column("is_regex", sa.Boolean(), nullable=False, server_default="false"),
        sa.Column("description", sa.String(length=500), nullable=True),
        sa.Column(
            "created_by",
            postgresql.UUID(as_uuid=True),
            sa.ForeignKey("users.id", ondelete="SET NULL"),
            nullable=True,
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )


def downgrade() -> None:
    # DISCLOSED: same "a new enum value can't be un-added" posture as
    # every prior migration in this chain -- only the real, cleanly
    # reversible table creation is undone.
    op.drop_table("blacklisted_patterns")
