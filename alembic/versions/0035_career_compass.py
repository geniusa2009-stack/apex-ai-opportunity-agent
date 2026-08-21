"""Career Compass -- real RIASEC (Holland Code) interest-inventory results.

Adds `career_compass_results`, a one-row-per-student table (same 1:1
shape as `gamification_profiles`) storing a student's most recent
submission: their raw 10 answers, per-dimension scores, Holland code,
match percentage, and matched career/major suggestions. A resubmission
("Retake the quiz") overwrites this row -- see
`app/models/career_compass.py` for the full disclosed reasoning.

Revision ID: 0035_career_compass
Revises: 0034_gamif_alias_monthly
Create Date: 2026-08-22
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

revision: str = "0035_career_compass"
down_revision: str | None = "0034_gamif_alias_monthly"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "career_compass_results",
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("answers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("dimension_scores", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("holland_code", sa.String(length=6), nullable=False),
        sa.Column("match_percentage", sa.Float(), nullable=False),
        sa.Column("matched_careers", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("user_id"),
    )


def downgrade() -> None:
    op.drop_table("career_compass_results")
