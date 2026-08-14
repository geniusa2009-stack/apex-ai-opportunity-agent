"""Real Leaderboard System, Level 2 -- gamification_profiles.monthly_xp
and .display_alias.

Revision ID: 0034_gamif_alias_monthly
Revises: 0033_cohorts_leagues_notif
Create Date: 2026-08-13

Two independent column additions on the same table, bundled in one
migration since neither needs a data backfill beyond a server_default --
same "unrelated-but-small, bundle them" posture as 0033's own three-part
bundle.

DISCLOSED, MATCHING PRECEDENT: this revision id is 24 characters, well
under the 32-character `alembic_version.version_num` width that
0033's own docstring documents hitting with its original (34-char) id --
kept short deliberately from the start rather than relying on a later fix.

- `gamification_profiles.monthly_xp` (int, default 0) -- a THIRD
  independent XP counter alongside the existing `total_xp` (all-time) and
  `weekly_xp` (resets Monday). See `app/models/badges.py`'s own comment
  on this column for why no monthly LEAGUE/promotion concept was added
  alongside it.
- `gamification_profiles.display_alias` (nullable string, no default --
  NULL means "no alias set, show my real name") -- optional leaderboard
  pseudonym. See that same model comment for why this lives on
  `GamificationProfile`, not `User`.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0034_gamif_alias_monthly"
down_revision: str | None = "0033_cohorts_leagues_notif"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gamification_profiles",
        sa.Column("monthly_xp", sa.Integer(), nullable=False, server_default="0"),
    )
    op.add_column(
        "gamification_profiles",
        sa.Column("display_alias", sa.String(length=60), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("gamification_profiles", "display_alias")
    op.drop_column("gamification_profiles", "monthly_xp")
