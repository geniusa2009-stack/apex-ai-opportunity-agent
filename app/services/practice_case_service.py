"""Training / Practice Mode for New Volunteers -- service layer.

THIS MODULE IS THE REAL AIRTIGHT-BOUNDARY GUARANTEE (Step 3's own top
priority, see `app/models/practice_case.py`'s own module docstring,
STEP 3.1, for the full design reasoning). Read this file's own imports
first: there is no import here of `moderation_log_service`,
`notification_service`, `chat_security`, any R2/storage client, any
Celery task, or any of `StudentVideo`/`ChatMessageReport`/
`GuildContentReport`/`ModerationLogEntry`. This is not an oversight to
audit for -- it is the actual safety mechanism. A practice action is
STRUCTURALLY INCAPABLE of sending a real notification, applying a real
ban, or deleting real content, because the only two things this module
ever does are (1) read the fixed, in-memory `PRACTICE_CASES` tuple and
(2) read/write the one real, isolated `practice_case_attempts` table.
There is no code path from here into any real mutation function at all
-- not "a flag that gates one," an absence of the call itself.
`tests/test_practice_cases.py::TestBoundaryNeverTouchesRealSideEffects`
is where this is actually, adversarially proven (patches every real
side-effect function this codebase has and asserts none of them are
ever called across every real practice action).
"""

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import AdminRole
from app.models.practice_case import (
    PRACTICE_ACTION_MIN_ROLE,
    PRACTICE_ACTIONS_BY_CASE_TYPE,
    PRACTICE_CASE_KEYS,
    PRACTICE_CASES,
    PRACTICE_CASES_BY_KEY,
    PracticeCaseAttempt,
)


class PracticeCaseError(Exception):
    def __init__(self, status_code: int, detail: str) -> None:
        self.status_code = status_code
        self.detail = detail
        super().__init__(detail)


async def load_attempts(db: AsyncSession, *, user_id: uuid.UUID) -> dict[str, PracticeCaseAttempt]:
    result = await db.execute(select(PracticeCaseAttempt).where(PracticeCaseAttempt.user_id == user_id))
    return {row.case_key: row for row in result.scalars().all()}


def validate_action(*, case_key: str, action: str, actor_role: AdminRole) -> None:
    """Real, honest validation -- mirrors the REAL tier/action rules the
    corresponding real route enforces (see `app/models/practice_case.py`'s
    own `PRACTICE_ACTIONS_BY_CASE_TYPE`/`PRACTICE_ACTION_MIN_ROLE`
    docstring for why this is genuine orientation value). Raises
    `PracticeCaseError`, never silently accepts an invalid choice --
    this is real input validation, not a scored "correctness" check
    (Scope Decision 1): EVERY valid action for a case type is accepted
    and recorded identically, there is no "right" one among them.
    """
    case = PRACTICE_CASES_BY_KEY.get(case_key)
    if case is None:
        raise PracticeCaseError(404, "Unknown practice case")

    allowed = PRACTICE_ACTIONS_BY_CASE_TYPE[case.case_type]
    if action not in allowed:
        raise PracticeCaseError(
            422,
            f"'{action}' is not a real action for a {case.case_type} practice case "
            f"(allowed: {', '.join(allowed)}).",
        )

    min_role = PRACTICE_ACTION_MIN_ROLE[action]
    if actor_role.rank < min_role.rank:
        raise PracticeCaseError(
            403,
            f"Practicing '{action}' requires {min_role.value.replace('_', ' ')} access or higher in "
            "the real system -- this is the same real tier limit the actual route enforces "
            "(nothing was recorded).",
        )


async def record_attempt(
    db: AsyncSession, *, user_id: uuid.UUID, case_key: str, action: str, notes: str | None
) -> PracticeCaseAttempt:
    """Real UPSERT -- see `PracticeCaseAttempt`'s own docstring for why
    a re-attempt overwrites, not appends. The ONLY write this entire
    feature ever performs; no other table is touched."""
    existing = await db.execute(
        select(PracticeCaseAttempt).where(
            PracticeCaseAttempt.user_id == user_id, PracticeCaseAttempt.case_key == case_key
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        row = PracticeCaseAttempt(user_id=user_id, case_key=case_key, action_taken=action, notes=notes)
        db.add(row)
    else:
        # Real re-attempt -- the column's own `server_default=func.now()`
        # only fires on INSERT, so an UPDATE needs its real "now" set
        # explicitly here, the same way `DangerousActionConfirmation.
        # executed_at` is set in Python rather than relying on a DB-side
        # trigger this codebase doesn't use.
        row.action_taken = action
        row.notes = notes
        row.acted_at = dt.datetime.now(dt.timezone.utc)

    await db.commit()
    await db.refresh(row)
    return row


async def reset_attempt(db: AsyncSession, *, user_id: uuid.UUID, case_key: str) -> bool:
    """Real undo -- mirrors onboarding's own reversible-toggle
    symmetry. Returns whether a row genuinely existed to remove."""
    existing = await db.execute(
        select(PracticeCaseAttempt).where(
            PracticeCaseAttempt.user_id == user_id, PracticeCaseAttempt.case_key == case_key
        )
    )
    row = existing.scalar_one_or_none()
    if row is None:
        return False
    await db.delete(row)
    await db.commit()
    return True


def all_case_keys() -> frozenset[str]:
    return PRACTICE_CASE_KEYS


def case_count() -> int:
    return len(PRACTICE_CASES)
