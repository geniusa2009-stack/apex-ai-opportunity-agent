"""Export Moderation Log -- new `ModerationAction.ACTIVITY_LOG_EXPORT`
enum member.

Revision ID: 0045_activity_log_export
Revises: 0044_opportunity_review_gate
Create Date: 2026-08-31

Real, distinct gap confirmed by this task's own Step 1/3.3 investigation:
before this task, an Owner exporting the whole team's moderation log to
a file was itself unlogged -- ironic, given the entire point of this
log is auditability. `moderation_action` is a real native Postgres ENUM
type (created by migration 0038, extended by 0040/0041/0043/0044) --
same single-value `ALTER TYPE ... ADD VALUE IF NOT EXISTS` pattern those
migrations already established (Postgres has no "add several values"
single statement, and `ADD VALUE` cannot run in the same transaction as
a later statement that USES the new value -- this migration only adds
the value, never uses it, so it's safe as an ordinary single-transaction
migration).

No new `moderation_target_type` value is needed. An export has no
single natural "target row" the way every other logged action does
(it's a read over a whole date range, not a mutation of one record) --
this migration deliberately reuses the existing `USER` member,
target_id=<the exporting Owner's own id>, same "direct-target, no
outcome-JSON-extraction-needed" convention `CHAT_UNBAN`/`CHAT_AUTO_BAN`/
`CHAT_MANUAL_BAN` already established for actions whose real subject is
a user account. See `app/api/v1/endpoints/admin_activity_log.py`'s own
module docstring for the full reasoning.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0045_activity_log_export"
down_revision: str | None = "0044_opportunity_review_gate"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'activity_log_export'")


def downgrade() -> None:
    # Same disclosed limitation 0040/0041/0043/0044 already documented:
    # Postgres has no `ALTER TYPE ... DROP VALUE` at all, so this is a
    # deliberate no-op, matching established precedent.
    pass
