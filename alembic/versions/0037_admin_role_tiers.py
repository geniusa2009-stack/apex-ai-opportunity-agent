"""Real Tiered Role System -- `users.admin_role` (Owner / Senior
Moderator / Moderator).

Revision ID: 0037_admin_role_tiers
Revises: 0036_gender_age_range
Create Date: 2026-08-23

Confirmed from the immediately-preceding live-test step: `is_superuser`
is the sole admin signal, binary, with zero writable API to set it --
the only way today is direct DB access. This migration adds the real,
ordered, nullable `admin_role` column (see `AdminRole`'s own docstring
in `app/models/enums.py` for the full tier design and for how it
resolves against `is_superuser` WITHOUT becoming a second, disagreeing
source of truth).

Nullable, NO `server_default` -- deliberately different from
`0031_user_role`'s `role` column: `None` ("no elevated moderation tier")
is the correct, common state for nearly every row, not a placeholder
needing a non-null fallback. No backfill needed for the same reason.

Enum-type creation mechanics mirror `0031_user_role.py` exactly (same
file, same reasoning, same `checkfirst=True` / `create_type=False`
pairing).

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this
sandbox at authoring time).
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects import postgresql

revision: str = "0037_admin_role_tiers"
down_revision: str | None = "0036_gender_age_range"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Mirrors `app.models.enums.AdminRole`'s member VALUES exactly -- same
# local-literal-tuple convention as `0031_user_role.py`/`0026_academic_level.py`.
_ADMIN_ROLE_VALUES = ("moderator", "senior_moderator", "owner")


def upgrade() -> None:
    admin_role_type = postgresql.ENUM(*_ADMIN_ROLE_VALUES, name="admin_role")
    admin_role_type.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "users",
        Column(
            "admin_role",
            postgresql.ENUM(*_ADMIN_ROLE_VALUES, name="admin_role", create_type=False),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "admin_role")
    postgresql.ENUM(name="admin_role").drop(op.get_bind(), checkfirst=True)
