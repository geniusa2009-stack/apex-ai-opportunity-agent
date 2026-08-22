"""Real Age/Academic-Year/Gender Opportunity Filtering -- adds the
genuinely-missing pieces confirmed by this step's own investigation:
`User.gender`, `Opportunity.min_age`, `Opportunity.max_age`,
`Opportunity.target_gender`. `User.age`/`User.academic_level`/
`Opportunity.target_age_group`/`Opportunity.target_academic_levels`
already existed (migrations 0001/0020/0026) -- not touched here.

Revision ID: 0036_gender_age_range
Revises: 0035_career_compass
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "0036_gender_age_range"
down_revision: str | None = "0035_career_compass"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_GENDER_ENUM = sa.Enum("male", "female", name="gender")


def upgrade() -> None:
    # Created once here (users.gender is the first column to use it);
    # opportunities.target_gender below reuses the same Postgres enum
    # type via create_type=False, matching the existing
    # academic_level/target_academic_levels precedent (migration 0026).
    _GENDER_ENUM.create(op.get_bind(), checkfirst=True)

    op.add_column("users", sa.Column("gender", _GENDER_ENUM, nullable=True))

    op.add_column("opportunities", sa.Column("min_age", sa.Integer(), nullable=True))
    op.add_column("opportunities", sa.Column("max_age", sa.Integer(), nullable=True))
    op.add_column(
        "opportunities",
        sa.Column("target_gender", sa.Enum("male", "female", name="gender", create_type=False), nullable=True),
    )


def downgrade() -> None:
    op.drop_column("opportunities", "target_gender")
    op.drop_column("opportunities", "max_age")
    op.drop_column("opportunities", "min_age")
    op.drop_column("users", "gender")
    _GENDER_ENUM.drop(op.get_bind(), checkfirst=True)
