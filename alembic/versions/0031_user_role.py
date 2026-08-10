"""User Directory Search -- `users.role` (backs the search endpoint's
`role` filter).

Revision ID: 0031_user_role
Revises: 0030_user_search_vector
Create Date: 2026-07-20

Disclosed, necessary addition -- see `app/models/enums.py`'s `UserRole`
docstring for the full reasoning: no role concept existed anywhere on
`User` before this, and the User Directory Search endpoint's own ask
names a `role` filter parameter that needs a real column to filter
against. One new native Postgres ENUM type (`user_role`, two values:
`student`, `mentor`) and one new NOT NULL column with a `STUDENT`
default, so every existing row is valid the moment this migration runs
(no backfill script needed).

Enum-type creation mechanics mirror `0026_academic_level.py` exactly
(same file, same reasoning): the type is created explicitly via
`postgresql.ENUM(...).create(..., checkfirst=True)` before the
`add_column` call, with `create_type=False` on the column definition so
SQLAlchemy doesn't try to `CREATE TYPE user_role` a second time.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this
sandbox).
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column
from sqlalchemy.dialects import postgresql

revision: str = "0031_user_role"
down_revision: str | None = "0030_user_search_vector"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Mirrors `app.models.enums.UserRole`'s member VALUES exactly -- kept as
# a local, literal tuple rather than importing the Python enum, matching
# this project's own established migration-authoring convention (see
# `0026_academic_level.py`'s own comment on why).
_USER_ROLE_VALUES = ("student", "mentor")


def upgrade() -> None:
    user_role_type = postgresql.ENUM(*_USER_ROLE_VALUES, name="user_role")
    user_role_type.create(op.get_bind(), checkfirst=True)

    op.add_column(
        "users",
        Column(
            "role",
            postgresql.ENUM(*_USER_ROLE_VALUES, name="user_role", create_type=False),
            nullable=False,
            server_default="student",
        ),
    )


def downgrade() -> None:
    op.drop_column("users", "role")
    postgresql.ENUM(name="user_role").drop(op.get_bind(), checkfirst=True)
