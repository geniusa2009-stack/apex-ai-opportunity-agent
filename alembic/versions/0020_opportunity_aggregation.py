"""Hybrid Opportunity Aggregation Engine.

Revision ID: 0020_opportunity_aggregation
Revises: 0019_video_analytics
Create Date: 2026-07-18

Three schema changes, all disclosed at length in `app/models/source.py`'s
and `app/models/opportunity.py`'s own module docstrings -- summarized here:

- `sources.source_type` (new column, String(20), backed by the new
  `SourceType` Python enum -- `app/models/enums.py`): `server_default=
  'website'` so every PRE-EXISTING `Source` row (all created by the
  Scrapy-based website scraper, `app/scraper/pipelines.py`) gets the
  objectively correct value with no separate backfill script needed.

- `opportunities.is_local_egypt` (new column, Boolean, `server_default=
  false`) and `opportunities.target_age_group` (new column, String(50),
  nullable) -- both genuinely new concepts, added exactly as this step's
  ask specifies.

- `opportunities.country` gets a NEW `server_default='Global'` (the
  column itself already existed, nullable, no default) -- applies to
  future inserts only; existing NULL rows are deliberately NOT backfilled
  (a live default and a historical-data rewrite are different asks, only
  the former was requested).

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0020_opportunity_aggregation"
down_revision: str | None = "0019_video_analytics"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "sources",
        sa.Column("source_type", sa.String(length=20), nullable=False, server_default="website"),
    )

    op.add_column(
        "opportunities",
        sa.Column("is_local_egypt", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column(
        "opportunities",
        sa.Column("target_age_group", sa.String(length=50), nullable=True),
    )
    op.create_index(
        "ix_opportunities_is_local_egypt", "opportunities", ["is_local_egypt"], unique=False
    )
    op.alter_column("opportunities", "country", server_default="Global")


def downgrade() -> None:
    op.alter_column("opportunities", "country", server_default=None)
    op.drop_index("ix_opportunities_is_local_egypt", table_name="opportunities")
    op.drop_column("opportunities", "target_age_group")
    op.drop_column("opportunities", "is_local_egypt")
    op.drop_column("sources", "source_type")
