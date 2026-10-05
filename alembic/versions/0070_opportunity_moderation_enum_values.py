"""Add the missing native-enum values used by opportunity moderation.

Revision ID: 0070_opp_moderation_enums
Revises: 0069_fix_users_search_trigger
Create Date: 2026-10-05

Found by the AI Opportunity Agent's Postgres integration tests: the Python
enums declare `ModerationAction.OPPORTUNITY_APPROVE` / `OPPORTUNITY_REJECT`
and `ModerationTargetType.OPPORTUNITY` (used since the "review of aggregated
opportunities before publish" feature), but no migration ever added them to
the native Postgres types `moderation_action` / `moderation_target_type`.
On a database built from the migrations, writing those log entries fails
with `invalid input value for enum`. The SQLite test-suite cannot see this
(it stores enums as plain text).

`ADD VALUE IF NOT EXISTS` makes this safe on databases where someone
already added the values by hand.

Downgrade: Postgres cannot drop an enum value; same disclosed posture as
the earlier migrations that add enum values (no-op).
"""

from collections.abc import Sequence

from alembic import op

revision = "0070_opp_moderation_enums"
down_revision = "0069_fix_users_search_trigger"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'opportunity_approve'")
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'opportunity_reject'")
    op.execute("ALTER TYPE moderation_target_type ADD VALUE IF NOT EXISTS 'opportunity'")


def downgrade() -> None:
    # Postgres has no ALTER TYPE ... DROP VALUE; leaving the values is harmless.
    pass
