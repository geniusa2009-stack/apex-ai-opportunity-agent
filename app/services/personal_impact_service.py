"""Opportunity Monitoring & Personal Impact Engine, Parts G/H/T: "who
cares?" -- given ONE opportunity that just had a verified, recorded
change, find the real, already-known users this affects, and produce an
explainable, non-fabricated reason for each.

REUSES, NEVER DUPLICATES: `UserBookmark` (saved/applied) and
`ApplicationTracker` (opportunity_id-linked, from Application
Intelligence) are the two existing tables this codebase already uses to
represent "a user has a real relationship to this opportunity" --
`guidance_service.py`'s own `_changed_deadline_guidance`/`
_application_change_impact_guidance` already establish this exact
candidate-selection shape for the deadline field alone; this module
generalizes it to any monitored field and centralizes it so both
guidance AND notifications (Part M) share one "who is affected" query
rather than two that could silently disagree about who gets told.

PERFORMANCE (Part T): two indexed queries total (one on `UserBookmark.
opportunity_id`, one on `ApplicationTracker.opportunity_id`, both
already-indexed FKs) per changed opportunity -- never "for every
changed opportunity: scan every user." No per-user LLM call anywhere in
this module.

PRIVACY (Part H/S): reasons are template strings built from real,
already-owned rows -- never a sensitive user attribute, never an
invented reason, never leaked across users (each `AffectedUser`'s
`reasons` describe only that user's own real relationship to the
opportunity).
"""

from __future__ import annotations

import dataclasses
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_tracker import ApplicationTracker
from app.models.bookmark import UserBookmark
from app.models.enums import BookmarkStatus
from app.services.application_state_machine import INCOMPLETE_TRACKER_STATUSES


@dataclasses.dataclass
class ImpactReason:
    category: str  # "saved" | "applied" | "active_application"
    text: str


@dataclasses.dataclass
class AffectedUser:
    user_id: uuid.UUID
    reasons: list[ImpactReason]
    has_active_application: bool


async def find_affected_users(db: AsyncSession, *, opportunity_id: uuid.UUID) -> list[AffectedUser]:
    """Part G: the ONLY three real, existing signals this codebase can
    honestly use to say "this user cares" about a specific opportunity --
    saved it, marked it applied via a bookmark, or is tracking a real
    application against it. Deliberately does NOT include "viewed once"
    (`OpportunityInteraction` VIEWED events) -- Part G's own explicit
    "do not automatically notify... everyone who viewed it once" rule;
    a bare view is exactly the weak signal that instruction singles out.
    "Explicitly marked relevant" / "currently highly relevant" (Part G's
    own items 4/5) are NOT implemented here -- see the report's Known
    Limitations: no existing, reliable, already-persisted signal for
    "currently highly relevant" exists independent of a live ranking
    call, and running personalized ranking per changed opportunity would
    violate Part T's own "no LLM/heavy call per user" performance rule.
    """
    reasons_by_user: dict[uuid.UUID, list[ImpactReason]] = {}
    active_app_users: set[uuid.UUID] = set()

    bookmark_result = await db.execute(
        select(UserBookmark).where(UserBookmark.opportunity_id == opportunity_id)
    )
    for bookmark in bookmark_result.scalars().all():
        if bookmark.status == BookmarkStatus.APPLIED.value:
            text = "You marked this opportunity as applied."
            category = "applied"
        else:
            text = "You saved this opportunity."
            category = "saved"
        reasons_by_user.setdefault(bookmark.user_id, []).append(ImpactReason(category=category, text=text))

    tracker_result = await db.execute(
        select(ApplicationTracker).where(ApplicationTracker.opportunity_id == opportunity_id)
    )
    for tracker in tracker_result.scalars().all():
        if tracker.status in INCOMPLETE_TRACKER_STATUSES:
            reasons_by_user.setdefault(tracker.user_id, []).append(
                ImpactReason(category="active_application", text="You have an active application to this opportunity.")
            )
            active_app_users.add(tracker.user_id)

    return [
        AffectedUser(user_id=user_id, reasons=reasons, has_active_application=user_id in active_app_users)
        for user_id, reasons in reasons_by_user.items()
    ]
