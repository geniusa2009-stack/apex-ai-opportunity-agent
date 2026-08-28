"""Manual Timed Bans -- new `ModerationAction.CHAT_MANUAL_BAN` enum
member.

Revision ID: 0043_manual_timed_bans
Revises: 0042_moderation_appeals
Create Date: 2026-08-28

Real, distinct gap confirmed by this task's own Step 1.3 investigation:
before this task, the only way a chat ban was ever applied was the
automatic Arabic-filter trigger (`CHAT_AUTO_BAN`) or escalating an
existing chat-message report (`CHAT_REPORT_ESCALATE_BAN`) -- there was
no direct, moderator-initiated, report-free "ban this user" action
anywhere. `moderation_action` is a real native Postgres ENUM type
(created by migration 0038, extended by 0040/0041) -- same single-value
`ALTER TYPE ... ADD VALUE IF NOT EXISTS` pattern those migrations
already established (Postgres has no "add several values" single
statement, and `ADD VALUE` cannot run in the same transaction as a later
statement that USES the new value -- this migration only adds the value,
never uses it, so it's safe as an ordinary single-transaction migration).
No new `moderation_target_type` value is needed -- the new action targets
the banned user directly (`target_type=USER`), an existing member.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0043_manual_timed_bans"
down_revision: str | None = "0042_moderation_appeals"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'chat_manual_ban'")


def downgrade() -> None:
    # Same disclosed limitation 0040/0041 already documented: Postgres
    # has no `ALTER TYPE ... DROP VALUE` at all (the only way to truly
    # remove an enum value is rebuilding the type), so this is a
    # deliberate no-op, matching established precedent rather than
    # attempting a destructive type rebuild for a downgrade path that
    # would need to first prove no row uses the value.
    pass
