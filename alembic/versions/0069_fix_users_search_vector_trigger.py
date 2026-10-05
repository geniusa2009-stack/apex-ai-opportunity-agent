"""Fix the `users_search_vector_trigger()` function created by 0030.

Revision ID: 0069_fix_users_search_trigger
Revises: 0068_ai_opportunity_agent
Create Date: 2026-10-05

Found while running the AI Opportunity Agent's Postgres integration tests
against a database built from these migrations: the trigger function from
0030 references the bare column names `full_name`, `skills` and `bio`
instead of `NEW.full_name`, `NEW.skills` and `NEW.bio`. Inside PL/pgSQL
those bare names are not row fields, so EVERY `INSERT INTO users` (and every
update of those columns) fails with `column "full_name" does not exist` on a
database built from the migration chain.

0030 itself is not rewritten (history stays as it was); this migration
replaces only the function body (`CREATE OR REPLACE FUNCTION`). The trigger
definition, the column and the index are untouched. Safe to run on a
database where the function was already fixed by hand: the result is the
same function.
"""

from collections.abc import Sequence

from alembic import op

revision = "0069_fix_users_search_trigger"
down_revision = "0068_ai_opportunity_agent"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

_FIXED = """
CREATE OR REPLACE FUNCTION users_search_vector_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
        setweight(to_tsvector('simple', coalesce(NEW.full_name, '')), 'A') ||
        setweight(to_tsvector('simple', coalesce(array_to_string(NEW.skills, ' '), '')), 'B') ||
        setweight(to_tsvector('simple', coalesce(NEW.bio, '')), 'C');
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""

# Exactly what 0030 created (kept only so a downgrade is a faithful reversal).
_ORIGINAL_0030 = """
CREATE OR REPLACE FUNCTION users_search_vector_trigger() RETURNS trigger AS $$
BEGIN
    NEW.search_vector :=
    setweight(to_tsvector('simple', coalesce(full_name, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(array_to_string(skills, ' '), '')), 'B') ||
    setweight(to_tsvector('simple', coalesce(bio, '')), 'C')
;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql
"""


def upgrade() -> None:
    op.execute(_FIXED)


def downgrade() -> None:
    op.execute(_ORIGINAL_0030)
