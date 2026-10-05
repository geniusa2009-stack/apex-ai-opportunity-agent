"""APEX Onboarding Production Integration -- adds four nullable profile
columns to `users` needed to persist the real 6-step onboarding, closing
the genuine gaps found during reconnaissance (no exact date_of_birth, no
English-proficiency field, no way to preserve more than one nationality,
no server-set onboarding-completion marker). No existing column's type,
nullability, or data is touched.

Revision ID: 0067_onboarding_profile_fields
Revises: 0066_user_privacy_settings
Create Date: 2026-10-03

Purely additive:
- `date_of_birth` (DATE, nullable) -- authoritative source of age for
  onboarding/eligibility going forward; the pre-existing `age` (Integer)
  column is left exactly as-is for any existing caller.
- `english_level` (new native enum `english_level`: basic/intermediate/
  advanced/fluent, nullable) -- genuinely missing before this step
  (confirmed by repository search; see `EnglishLevel`'s own docstring).
- `other_nationalities` (ARRAY(VARCHAR(120)), nullable) -- preserves any
  nationality beyond the existing single `nationality` column, so the
  onboarding UI's multi-select never silently discards data.
- `onboarding_completed_at` (TIMESTAMPTZ, nullable) -- the one real,
  server-set completion marker; `NULL` for every pre-existing user.

All four are nullable with no default, so every existing row is valid
immediately after this migration runs -- no backfill, no data rewrite,
no destructive change.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0067_onboarding_profile_fields"
down_revision = "0066_user_privacy_settings"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

ENGLISH_LEVEL_VALUES = ("basic", "intermediate", "advanced", "fluent")


def upgrade() -> None:
    english_level_enum = postgresql.ENUM(
        *ENGLISH_LEVEL_VALUES, name="english_level", create_type=False
    )
    english_level_enum.create(op.get_bind(), checkfirst=True)

    op.add_column("users", sa.Column("date_of_birth", sa.Date(), nullable=True))
    op.add_column(
        "users",
        sa.Column("english_level", english_level_enum, nullable=True),
    )
    op.add_column(
        "users",
        sa.Column(
            "other_nationalities",
            postgresql.ARRAY(sa.String(length=120)),
            nullable=True,
        ),
    )
    op.add_column(
        "users",
        sa.Column(
            "onboarding_completed_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "onboarding_completed_at")
    op.drop_column("users", "other_nationalities")
    op.drop_column("users", "english_level")
    op.drop_column("users", "date_of_birth")

    english_level_enum = postgresql.ENUM(
        *ENGLISH_LEVEL_VALUES, name="english_level", create_type=False
    )
    english_level_enum.drop(op.get_bind(), checkfirst=True)
