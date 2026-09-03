"""Manual Opportunity Creation -- add the new `opportunity_manual_create`
value to the real native Postgres `moderation_action` enum type.

Revision ID: 0050_opportunity_manual_create_action
Revises: 0049_shift_assignments
Create Date: 2026-09-03

SELF-CORRECTION: the module docstring comment originally added alongside
`ModerationAction.OPPORTUNITY_MANUAL_CREATE` (app/models/enums.py)
claimed no migration was needed, reasoning that `ModerationAction` "backs
a plain String column, not a native Postgres ENUM type" -- confirmed
FALSE on a full re-read of `app/models/moderation_log.py`:
`ModerationLogEntry.action` is `SAEnum(ModerationAction, name=
"moderation_action", native_enum=True, ...)`, a real native Postgres
ENUM (see migrations 0045/0046/0047's own `ALTER TYPE moderation_action
ADD VALUE` precedent for every other addition to this same type). Caught
before this reached a real Postgres deployment -- inserting a
`ModerationLogEntry` row with `action="opportunity_manual_create"`
against real Postgres without this migration would fail with
`invalid input value for enum moderation_action`, even though the
in-memory-SQLite test suite this codebase's own tests run against never
catches this class of bug (SQLite has no real ENUM type to validate
against, so an unmigrated enum value silently "works" there). The
incorrect docstring claim in `app/models/enums.py` is corrected in the
same commit that adds this migration.

`SourceType`/`Source.source_type` is NOT affected -- that really is a
plain `String(20)` column (`app/models/source.py`'s own column
definition), no `SAEnum`/`native_enum` involved, so `SourceType.MANUAL`
needed no migration, exactly as originally reasoned.
"""

from collections.abc import Sequence

from alembic import op

revision: str = "0050_opportunity_manual_create_action"
down_revision: str | None = "0049_shift_assignments"
branch_labels: Sequence[str] | None = None
depends_on: Sequence[str] | None = None


def upgrade() -> None:
    op.execute("ALTER TYPE moderation_action ADD VALUE IF NOT EXISTS 'opportunity_manual_create'")


def downgrade() -> None:
    # Postgres has no `ALTER TYPE ... DROP VALUE` -- same accepted,
    # disclosed limitation every prior enum-value-adding migration in
    # this codebase (0041/0045/0046/0047) already leaves undone; a
    # value, once added, stays in the type for the life of the database.
    pass
