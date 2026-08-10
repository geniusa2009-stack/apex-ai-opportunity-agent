"""User Directory Search -- schema + native Postgres FTS trigger.

Revision ID: 0030_user_search_vector
Revises: 0029_live_session_reminders
Create Date: 2026-07-20

Second disclosed gap closed from the Live 1:1 Video Sessions step's own
README entry: "no user-search/directory endpoint... no user-search/
people-directory endpoint exists anywhere in this codebase." This
migration is the schema/trigger groundwork ONLY -- no search endpoint is
built in this pass (that's this step's own explicitly-stated "next"
work); see `app/models/user.py`'s own comments on `bio`/`search_vector`
for the full column-level reasoning.

Adds, to `users`:
- `bio` -- plain nullable `TEXT`. A genuinely NEW column (see
  `app/models/user.py`); ordinary `op.add_column`.
- `search_vector` -- nullable `TSVECTOR`, maintained ENTIRELY by a native
  Postgres trigger, never written to by application code.
- A trigger function (`users_search_vector_trigger`) + `BEFORE INSERT OR
  UPDATE OF full_name, skills, bio` trigger (`users_search_vector_update`)
  that keeps `search_vector` in sync automatically -- no application-code
  code path is responsible for updating it; it is correct by construction
  on every relevant write, including ones this codebase's own future code
  hasn't been written yet (a raw `UPDATE users SET bio = ...` from a
  future admin tool, a future bulk-import script, etc. would all still
  keep `search_vector` correct, which an application-level "recompute
  after save" hook would NOT guarantee for write paths nobody remembered
  to wire it into).
- A `GIN` index on `search_vector` for fast `@@` full-text queries.
- A one-time backfill `UPDATE` so any user rows that already exist at
  migration time get a correct `search_vector` immediately -- DISCLOSED,
  NECESSARY: `ALTER TABLE ... ADD COLUMN` does NOT retroactively fire a
  trigger for pre-existing rows (the trigger only fires on a NEW insert/
  update event going forward), so without this explicit backfill, every
  user who signed up before this migration ran would be permanently
  unsearchable until they happened to next update their own profile.

DISCLOSED JUDGMENT CALL -- text search configuration is `'simple'`, NOT
`'english'` (the illustrative default most FTS tutorials, and this step's
own ask, would reach for first). This platform's own established
precedent (`app/services/chat_security.py`'s Arabic-script detection,
the many Arabic-language AI prompts throughout this codebase) makes clear
real user-entered profile text here -- names, bios -- is expected to be a
genuine mix of Arabic and English, sometimes within the same field.
Postgres DOES ship a real `'arabic'` text search configuration, but
picking EITHER `'english'` OR `'arabic'` would apply that one language's
stemming rules to text that may not be in that language at all, which is
worse than applying no language-specific stemming. `'simple'` still
lowercases and tokenizes (so `"Cairo"` and `"cairo"` still match, and
Arabic text is still indexed and matchable), it just skips language-
specific stemming (an English "engineer"/"engineering" won't be treated
as the same lexeme under `'simple'` the way `'english'` would) -- a
disclosed, deliberate trade-off favoring not corrupting non-English
content over slightly weaker English recall, appropriate for a genuinely
bilingual user base.

DISCLOSED JUDGMENT CALL -- field-to-weight mapping. This ask's own
"name/tags/bio" -> A/B/C example doesn't map onto this codebase's actual
column names 1:1: `full_name` is the real "name" column (weight A);
`skills` (a Postgres `ARRAY(String)`, this model's own existing closest
analog to "tags") is weight B, flattened via `array_to_string(..., ' ')`
before being tokenized (`to_tsvector` operates on `text`, not `text[]`);
`bio` (a brand-new column -- see `app/models/user.py`) is weight C.

Hand-written and reviewed via `alembic upgrade head --sql` /
`alembic downgrade -1 --sql`, matching this project's established
migration-authoring posture (no live Postgres available in this
sandbox) -- genuinely important here specifically, since `--sql` mode is
the only way in this sandbox to confirm the trigger/function DDL syntax
is actually valid Postgres before it ever runs against a real database.
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "0030_user_search_vector"
down_revision: str | None = "0029_live_session_reminders"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None

# Shared between the trigger function body and the one-time backfill
# UPDATE below -- kept as a single source of truth in this migration
# (both must compute `search_vector` identically, or the backfilled rows
# and every row touched after this migration runs would disagree with
# each other for no reason).
_SEARCH_VECTOR_EXPRESSION = """
    setweight(to_tsvector('simple', coalesce(full_name, '')), 'A') ||
    setweight(to_tsvector('simple', coalesce(array_to_string(skills, ' '), '')), 'B') ||
    setweight(to_tsvector('simple', coalesce(bio, '')), 'C')
"""


def upgrade() -> None:
    # --- 1. New columns -------------------------------------------------
    op.add_column("users", sa.Column("bio", sa.Text(), nullable=True))
    op.add_column("users", sa.Column("search_vector", postgresql.TSVECTOR(), nullable=True))

    # --- 2. Trigger function + trigger ----------------------------------
    # `NEW.search_vector := ...` inside a BEFORE trigger mutates the row
    # being written before it's actually persisted -- no second UPDATE,
    # no risk of a caller's own concurrent write racing against it the
    # way an AFTER-trigger-issues-its-own-UPDATE approach would.
    op.execute(
        f"""
        CREATE OR REPLACE FUNCTION users_search_vector_trigger() RETURNS trigger AS $$
        BEGIN
            NEW.search_vector := {_SEARCH_VECTOR_EXPRESSION};
            RETURN NEW;
        END;
        $$ LANGUAGE plpgsql
        """
    )
    op.execute(
        """
        CREATE TRIGGER users_search_vector_update
            BEFORE INSERT OR UPDATE OF full_name, skills, bio ON users
            FOR EACH ROW
            EXECUTE FUNCTION users_search_vector_trigger()
        """
    )

    # --- 3. GIN index -----------------------------------------------------
    op.execute("CREATE INDEX ix_users_search_vector ON users USING GIN (search_vector)")

    # --- 4. One-time backfill for rows that predate this migration -----
    # See this migration's own module docstring for why this is
    # necessary (ADD COLUMN does not retroactively fire the trigger).
    op.execute(f"UPDATE users SET search_vector = {_SEARCH_VECTOR_EXPRESSION}")


def downgrade() -> None:
    # Reverse order of creation: index -> trigger -> function -> columns.
    op.execute("DROP INDEX IF EXISTS ix_users_search_vector")
    op.execute("DROP TRIGGER IF EXISTS users_search_vector_update ON users")
    op.execute("DROP FUNCTION IF EXISTS users_search_vector_trigger()")
    op.drop_column("users", "search_vector")
    op.drop_column("users", "bio")
