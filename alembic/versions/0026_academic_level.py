"""Backend Folder Structure & Core Database Models -- academic level.

Revision ID: 0026_academic_level
Revises: 0025_dashboards
Create Date: 2026-07-18

Three additive columns, all disclosed at length in `app/models/enums.py`'s
(`AcademicLevel`), `app/models/user.py`'s, and `app/models/opportunity.py`'s
own module/field docstrings -- summarized here:

- A NEW native Postgres ENUM type, `academic_level` (14 values: Prep
  1-3, Secondary 1-3, University Year 1-6, Masters, PhD).
- `users.age` (new column, Integer, nullable) -- a genuinely new numeric
  field; nothing previously captured an actual age.
- `users.academic_level` (new column, uses the new enum type, nullable).
- `opportunities.target_academic_levels` (new column, ARRAY of the same
  enum type, nullable) -- for strict, per-level opportunity filtering,
  ADDITIVE alongside the pre-existing `target_age_group` free-text
  column, not a replacement for it.

Enum-type creation, disclosed mechanics: unlike `opportunity_type`/
`bookmark_status` (migration 0001), which were created implicitly as
part of `op.create_table(...)` (SQLAlchemy auto-emits `CREATE TYPE`
when a brand-new `sa.Enum`-typed column is part of a `CREATE TABLE`),
`academic_level` is being added to two ALREADY-EXISTING tables via
`op.add_column`, and it backs TWO separate columns in this same
migration (`users.academic_level` directly, `opportunities.
target_academic_levels` as an `ARRAY` of it). The type is therefore
created explicitly, exactly once, via `postgresql.ENUM(...).create(...,
checkfirst=True)` before either `add_column` call, and both column
definitions pass `create_type=False` so SQLAlchemy doesn't try to
`CREATE TYPE academic_level` a second time and fail on "type already
exists." `downgrade()` mirrors migration 0001's own `sa.Enum(name=...).
drop(bind, checkfirst=True)` posture -- dropping a column does NOT
implicitly drop a custom Postgres type the way dropping a table's last
enum-typed column doesn't either, so the type is dropped explicitly,
last, only after both columns that reference it are gone.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this sandbox).
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, Integer
from sqlalchemy.dialects import postgresql

revision: str = "0026_academic_level"
down_revision: str | None = "0025_dashboards"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Mirrors `app.models.enums.AcademicLevel`'s member VALUES exactly (a
# Postgres ENUM type is defined by its string labels, independent of the
# Python enum's member NAMES) -- kept as a local, literal tuple rather
# than importing the Python enum here, matching this project's own
# established migration-authoring convention (every earlier migration
# that creates a native enum type, e.g. 0001's `opportunity_type`/
# `bookmark_status`, spells the values out literally rather than
# importing `app.models.enums` into the migration -- migrations must
# stay correct forever even if the application-layer enum class is
# later renamed or its members reordered).
_ACADEMIC_LEVEL_VALUES = (
    "prep_1",
    "prep_2",
    "prep_3",
    "secondary_1",
    "secondary_2",
    "secondary_3",
    "university_year_1",
    "university_year_2",
    "university_year_3",
    "university_year_4",
    "university_year_5",
    "university_year_6",
    "masters",
    "phd",
)


def upgrade() -> None:
    academic_level_type = postgresql.ENUM(*_ACADEMIC_LEVEL_VALUES, name="academic_level")
    academic_level_type.create(op.get_bind(), checkfirst=True)

    op.add_column("users", Column("age", Integer(), nullable=True))
    op.add_column(
        "users",
        Column(
            "academic_level",
            postgresql.ENUM(*_ACADEMIC_LEVEL_VALUES, name="academic_level", create_type=False),
            nullable=True,
        ),
    )
    op.add_column(
        "opportunities",
        Column(
            "target_academic_levels",
            postgresql.ARRAY(
                postgresql.ENUM(*_ACADEMIC_LEVEL_VALUES, name="academic_level", create_type=False)
            ),
            nullable=True,
        ),
    )


def downgrade() -> None:
    op.drop_column("opportunities", "target_academic_levels")
    op.drop_column("users", "academic_level")
    op.drop_column("users", "age")

    postgresql.ENUM(name="academic_level").drop(op.get_bind(), checkfirst=True)
