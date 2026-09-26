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

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user_notification_preference import UserNotificationPreference

_CATEGORY_COLUMNS = {"opportunity_changes", "deadline_alerts", "application_alerts"}


async def get_or_create_preferences(db: AsyncSession, user_id: uuid.UUID) -> UserNotificationPreference:
    """Read-mostly lookup, same posture as `aggregator_service.get_or_
    create_source` -- returns the existing row if present, otherwise
    creates and flushes (not commits -- the caller decides transaction
    boundaries) a new, all-defaults-on row."""
    existing = await db.get(UserNotificationPreference, user_id)
    if existing is not None:
        return existing

    preference = UserNotificationPreference(user_id=user_id)
    db.add(preference)
    await db.flush()
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
