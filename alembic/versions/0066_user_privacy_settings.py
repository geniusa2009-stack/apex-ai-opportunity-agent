"""APEX Privacy + Social Identity Layer -- adds a minimal per-user
privacy-settings table (one visibility switch per profile data
category). No existing table or column is touched.

Revision ID: 0066_user_privacy_settings
Revises: 0065_opportunity_monitoring
Create Date: 2026-09-30

Purely additive: one new table, FK cascade to `users.id`, same shape as
`0065`'s own `user_notification_preferences` table. No existing column
type/nullability changed, no data migrated/rewritten. See
`app/models/user_privacy.py` for the full design reasoning (why these
five categories, and why they default to "public").
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0066_user_privacy_settings"
down_revision = "0065_opportunity_monitoring"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "user_privacy_settings",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("bio_visibility", sa.String(length=20), nullable=False, server_default="public"),
        sa.Column("education_visibility", sa.String(length=20), nullable=False, server_default="public"),
        sa.Column("skills_visibility", sa.String(length=20), nullable=False, server_default="public"),
        sa.Column("location_visibility", sa.String(length=20), nullable=False, server_default="public"),
        sa.Column("languages_visibility", sa.String(length=20), nullable=False, server_default="public"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
    )


def downgrade() -> None:
    op.drop_table("user_privacy_settings")
