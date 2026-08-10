"""Student Dashboard & Admin Control Panel -- users.age_group.

Revision ID: 0025_dashboards
Revises: 0024_deadline_reminder_flag
Create Date: 2026-07-18

This milestone is otherwise a pure aggregation layer (two new GET
endpoints reading data every prior migration already created) plus two
new admin action endpoints that mutate EXISTING columns
(`student_videos.status`/`hls_master_url`/`thumbnail_url`, already added
by migrations 0014/0016/0018/0017) -- neither needs new schema.

The one real, disclosed schema addition is `users.age_group`, needed so
the student hub's "Recommended Opportunities" widget has something to
compare against `Opportunity.target_age_group` with. See
`app/models/user.py`'s own comment on this column for the full
reasoning.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0025_dashboards"
down_revision: str | None = "0024_deadline_reminder_flag"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("users", sa.Column("age_group", sa.String(length=50), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "age_group")
