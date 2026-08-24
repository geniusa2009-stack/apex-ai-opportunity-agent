"""Repeat-Offender Detection -- `moderation_log_entries.actor_role`
becomes nullable (system-initiated log entries have no human actor),
plus a new `chat_auto_ban` `ModerationAction` enum value.

Revision ID: 0041_repeat_offender_detection
Revises: 0040_chat_message_reports
Create Date: 2026-08-24

See `app/models/moderation_log.py`'s own `actor_role` column docstring
for the full "why nullable, why not a new AdminRole member" reasoning,
and `ModerationAction.CHAT_AUTO_BAN`'s own docstring for the real,
confirmed gap this closes (the automatic Arabic-filter ban previously
wrote no durable log entry at all).
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0041_repeat_offender_detection"
down_revision: str | None = "0040_chat_message_reports"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'chat_auto_ban'")
    op.execute("ALTER TABLE moderation_log_entries ALTER COLUMN actor_role DROP NOT NULL")


def downgrade() -> None:
    # DISCLOSED: cannot cleanly re-add NOT NULL if any real
    # `chat_auto_ban` (actor_role IS NULL) row has been written since
    # upgrade -- same "new enum value can't be un-added, and a downgrade
    # that would corrupt real data isn't attempted" posture as migration
    # 0040's own downgrade. A real deployment rolling back past this
    # migration while system-initiated log rows exist would need a
    # manual data decision (delete them, or backfill a sentinel) this
    # migration does not make on its own.
    op.execute(
        "ALTER TABLE moderation_log_entries ALTER COLUMN actor_role SET NOT NULL"
    )
