"""APEX User Intelligence + Proactive Guidance step, Part D: recording
and reading explicit user->opportunity behavioral events.

BEHAVIORAL SIGNAL HIERARCHY (Part D's own explicit requirement --
"explicit negative feedback > explicit positive intent > application >
save > meaningful engagement > view", documented here and nowhere else,
never a hardcoded magic number without this comment):

    DISMISSED / MARKED_NOT_RELEVANT   (explicit negative feedback)
        outweighs
    SHARED / MORE_LIKE_THIS           (explicit positive intent)
        outweighs
    applied (UserBookmark.APPLIED)    (application)
        outweighs
    saved (UserBookmark.SAVED)        (save)
        outweighs
    VIEWED                            (meaningful engagement / view --
                                        this codebase has no dwell-time
                                        or scroll-depth instrumentation,
                                        so "meaningful engagement" and
                                        "view" collapse to the same
                                        signal here, disclosed rather
                                        than fabricated)

A VIEW is never treated as equivalent to a SAVE, and a SAVE is never
treated as equivalent to an APPLICATION -- each has a distinct, ordered
weight below, used only to rank/deprioritize within `opportunity_feed.py`
and `user_intelligence.py`, never to silently promote/demote hard
eligibility (that boundary lives entirely in `eligibility_engine.py` and
is untouched by anything in this module).
"""

from __future__ import annotations

import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import InteractionEventType
from app.models.opportunity_interaction import OpportunityInteraction

# Documented, reproducible weights -- Part S's "no score theater" applies
# here exactly as it does to `personalized_ranking.py`'s `_LEVEL_WEIGHT`.
# Negative values are explicit negative feedback; the two application-
# lifecycle rows are sourced from `UserBookmark.status`, not this table
# (see `InteractionEventType`'s own docstring for why).
# Tier magnitudes strictly follow the documented hierarchy above:
# negative feedback (-10, dominates by sign, not magnitude-vs-positives)
# > explicit positive intent (6.0) > application (5.0-5.5) > save (3.0)
# > view (1.0). `applied`/STARTED_APPLICATION and COMPLETED_APPLICATION
# sit in the same "application" tier (COMPLETED slightly above STARTED/
# applied, since finishing is further real progress than starting) --
# both stay BELOW the explicit-positive-intent tier, per the hierarchy's
# own literal ordering (a user explicitly saying "show me more like
# this" is a stronger, more deliberate signal than the mere act of
# applying, which can happen for one opportunity without the user
# wanting more of that same kind).
BEHAVIOR_SIGNAL_WEIGHT: dict[str, float] = {
    InteractionEventType.DISMISSED.value: -10.0,
    InteractionEventType.MARKED_NOT_RELEVANT.value: -10.0,
    InteractionEventType.SHARED.value: 6.0,
    InteractionEventType.MORE_LIKE_THIS.value: 6.0,
    InteractionEventType.COMPLETED_APPLICATION.value: 5.5,
    "applied": 5.0,  # UserBookmark.status == BookmarkStatus.APPLIED
    InteractionEventType.STARTED_APPLICATION.value: 5.0,
    "saved": 3.0,  # UserBookmark.status == BookmarkStatus.SAVED
    InteractionEventType.VIEWED.value: 1.0,
}


async def record_interaction(
    db: AsyncSession,
    *,
    user_id: uuid.UUID,
    opportunity_id: uuid.UUID,
    event_type: InteractionEventType,
    event_metadata: dict | None = None,
) -> OpportunityInteraction:
    """Append-only write -- never an UPDATE, never a deletion. Every call
    creates a new, distinct row even if the same user records the same
    event type against the same opportunity twice (e.g. two separate
    VIEWED events); this module does no deduplication, leaving that to
    any future analytics need that actually wants it."""
    row = OpportunityInteraction(
        user_id=user_id,
        opportunity_id=opportunity_id,
        event_type=event_type.value,
        event_metadata=event_metadata,
    )
    db.add(row)
    await db.flush()
    return row


async def get_dismissed_opportunity_ids(db: AsyncSession, *, user_id: uuid.UUID) -> set[uuid.UUID]:
    """Part O's own explicit feedback-loop requirement: explicit negative
    feedback must outweigh weak passive behavior. Used by
    `opportunity_feed.py` to exclude opportunities the user has already
    told APEX they don't want to see, rather than merely deprioritizing
    them alongside everything else."""
    result = await db.execute(
        select(OpportunityInteraction.opportunity_id).where(
            OpportunityInteraction.user_id == user_id,
            OpportunityInteraction.event_type.in_(
                [InteractionEventType.DISMISSED.value, InteractionEventType.MARKED_NOT_RELEVANT.value]
            ),
        )
    )
    return {row[0] for row in result.all()}


async def get_recent_interactions(
    db: AsyncSession, *, user_id: uuid.UUID, limit: int = 20
) -> list[OpportunityInteraction]:
    """This user's most recent explicit events, newest first -- used by
    `user_intelligence.py`'s "recent meaningful activity" section. Does
    NOT include SAVED/APPLIED bookmark events (see this module's own
    docstring on why those live in `UserBookmark` instead) -- a caller
    that wants a single merged activity timeline should combine this
    with the caller's own `user.bookmarks`, which `user_intelligence.py`
    does.
    """
    result = await db.execute(
        select(OpportunityInteraction)
        .where(OpportunityInteraction.user_id == user_id)
        .order_by(OpportunityInteraction.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars().all())
