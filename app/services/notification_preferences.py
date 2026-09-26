"""Opportunity Monitoring & Personal Impact Engine, Part O: the minimal
read/write layer for `UserNotificationPreference`.

Lazily creates a default (everything-on) row on first real check rather
than requiring one to exist -- see `UserNotificationPreference`'s own
docstring for why no backfill migration was written. Best-effort by
design: `is_opted_in` never raises for a user with no row; it simply
returns the column's own real default.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_notification_preference import UserNotificationPreference

_CATEGORY_COLUMNS = {"opportunity_changes", "deadline_alerts", "application_alerts"}


async def get_or_create_preferences(db: AsyncSession, user_id: uuid.UUID) -> UserNotificationPreference:
    """Read-mostly lookup, same posture as `aggregator_service.get_or_
    create_source` -- returns the existing row if present, otherwise
    creates and flushes (not commits -- the caller decides transaction
    boundaries) a new, all-defaults-on row.

    FULL INTELLIGENCE AUDIT FIX: `user_id` is this table's own primary
    key (`UserNotificationPreference.user_id`), so two concurrent
    first-time calls for the SAME user (this function's only real
    caller, `opportunity_change_notifications.py`'s per-user fan-out
    loop, can genuinely run this way if the same opportunity change
    happens to be processed for a user by two overlapping workers) used
    to race: both would pass the `db.get(...) is None` check, then the
    second `flush()` would raise an uncaught `IntegrityError`, which
    would surface as an unhandled failure and (per `opportunity_change_
    notifications.py`'s own lack of a try/except around this call) abort
    that user's entire notification fan-out for an otherwise-real,
    already-verified opportunity change.

    Fixed with the same catch-IntegrityError-and-refetch idea
    `bookmarks.py`'s own create-bookmark race guard already established
    for this exact class of race -- but via a SAVEPOINT (`db.begin_
    nested()`), not a full `db.rollback()`: this function runs deep
    inside `opportunity_change_notifications.py`'s per-user loop, in the
    SAME session/transaction that already holds an earlier, still-
    uncommitted, genuinely-verified `OpportunityChangeHistory` INSERT
    (flushed, not committed, by `record_verified_changes`). A bare
    `db.rollback()` here would discard that real, already-accepted
    change record too, for a completely unrelated preference-row race --
    exactly the kind of one-user-fails-corrupts-everyone-else failure
    mode Part V forbids. A SAVEPOINT scopes the rollback to only this
    function's own failed INSERT attempt."""
    existing = await db.get(UserNotificationPreference, user_id)
    if existing is not None:
        return existing

    try:
        async with db.begin_nested():
            preference = UserNotificationPreference(user_id=user_id)
            db.add(preference)
            await db.flush()
    except IntegrityError:
        existing = await db.get(UserNotificationPreference, user_id)
        if existing is not None:
            return existing
        raise
    return preference


async def is_opted_in(db: AsyncSession, user_id: uuid.UUID, category: str) -> bool:
    """Part O: "respect user opt-outs." `category` must be one of
    `_CATEGORY_COLUMNS` -- an unknown category raises rather than
    silently defaulting to `True`, since a typo'd category name should
    never accidentally bypass a real user opt-out."""
    if category not in _CATEGORY_COLUMNS:
        raise ValueError(f"unknown notification preference category {category!r}")
    preference = await get_or_create_preferences(db, user_id)
    return bool(getattr(preference, category))
