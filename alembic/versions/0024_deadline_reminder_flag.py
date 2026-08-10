"""Smart Notifications Engine -- Approaching Deadlines reminder flag.

Revision ID: 0024_deadline_reminder_flag
Revises: 0023_notifications
Create Date: 2026-07-18

Adds `deadline_alerts.reminder_sent` -- a disclosed schema addition to an
already-shipped model (Ecosystem Feature 2), NOT part of this step's own
`app/models/notification.py` spec. See `app/models/deadline_alert.py`'s
own comment on this column, and `app/workers/deadline_reminder_tasks.py`'s
module docstring, for the full reasoning behind why the "Approaching
Deadlines" notification trigger needs it.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0024_deadline_reminder_flag"
down_revision: str | None = "0023_notifications"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "deadline_alerts",
        sa.Column("reminder_sent", sa.Boolean(), nullable=False, server_default=sa.false()),
    )


def downgrade() -> None:
    op.drop_column("deadline_alerts", "reminder_sent")
