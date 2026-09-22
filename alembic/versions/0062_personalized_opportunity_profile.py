"""Personalized Opportunity Intelligence + Opportunity Passport -- Part A
(User Opportunity Profile).

Revision ID: 0062_personalized_opportunity_profile
Revises: 0061_temporal_state_and_change_history
Create Date: 2026-09-22

Nine additive, nullable columns on `users`, none of them touching any
existing column or row. No new table, no new Postgres enum type: the
one new closed vocabulary this phase introduces (`ApplicationGoal`, 7
values) is stored as a plain `ARRAY(String(30))` and validated at the
Pydantic schema layer (`UserProfileUpdate.application_goals`), not a
native Postgres enum -- see that enum's own docstring
(app/models/enums.py) for the full "why not a native enum column"
reasoning, mirroring `users.skills`'s own pre-existing
`ARRAY(String)` precedent rather than adding a fourth native-enum type
to this migration for one small, cheap-to-validate-in-Python list.

`preferred_opportunity_types` is the one column that DOES reuse an
existing native Postgres enum type (`opportunity_type`, already created
by migration 0001 and expanded to 24 values by migration
0060_opportunity_type_expansion) -- `create_type=False` throughout,
same established pattern as migration 0026's own
`opportunities.target_academic_levels` ARRAY-of-existing-enum column.

Hand-written and reviewed via syntax parse + manual pattern comparison
against migrations 0026/0036 (no live Postgres available in this
sandbox -- disclosed, established posture for this whole engagement).
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import Column, Integer, String
from sqlalchemy.dialects import postgresql

revision: str = "0062_personalized_opportunity_profile"
down_revision: str | None = "0061_temporal_state_and_change_history"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Mirrors `app.models.enums.OpportunityType`'s member VALUES exactly, as
# of the Taxonomy Evolution phase's 24-value expansion -- a literal
# tuple, not an import, matching this project's own established
# migration-authoring convention (see migration 0026's own docstring for
# why migrations never import `app.models.enums`).
_OPPORTUNITY_TYPE_VALUES = (
    "scholarship", "internship", "volunteer", "course",
    "fellowship", "grant", "research_funding", "research_program",
    "research_internship", "phd", "masters", "bachelors",
    "company_training", "job", "competition", "accelerator",
    "incubator", "startup_funding", "project_funding",
    "exchange_program", "conference", "youth_program",
    "certification", "other",
)


def upgrade() -> None:
    op.add_column("users", Column("nationality", String(120), nullable=True))
    op.add_column("users", Column("residency_country", String(120), nullable=True))
    op.add_column("users", Column("languages", postgresql.ARRAY(String(50)), nullable=True))
    op.add_column("users", Column("interests", postgresql.ARRAY(String(100)), nullable=True))
    op.add_column("users", Column("application_goals", postgresql.ARRAY(String(30)), nullable=True))
    op.add_column(
        "users",
        Column(
            "preferred_opportunity_types",
            postgresql.ARRAY(
                postgresql.ENUM(*_OPPORTUNITY_TYPE_VALUES, name="opportunity_type", create_type=False)
            ),
            nullable=True,
        ),
    )
    op.add_column("users", Column("remote_preference", postgresql.BOOLEAN(), nullable=True))
    op.add_column("users", Column("funding_preference", String(30), nullable=True))
    op.add_column("users", Column("deadline_lead_time_days", Integer(), nullable=True))


def downgrade() -> None:
    op.drop_column("users", "deadline_lead_time_days")
    op.drop_column("users", "funding_preference")
    op.drop_column("users", "remote_preference")
    op.drop_column("users", "preferred_opportunity_types")
    op.drop_column("users", "application_goals")
    op.drop_column("users", "interests")
    op.drop_column("users", "languages")
    op.drop_column("users", "residency_country")
    op.drop_column("users", "nationality")
