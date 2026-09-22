"""APEX User Intelligence + Proactive Guidance step, Part J/K: the first
real, deterministic GUIDANCE layer -- the differentiator between "APEX
recommends" and "APEX guides."

NO LLM AUTHORITY (Part R, same discipline as `eligibility_engine.py`):
every `GuidanceItem`/`NextActionItem` below is built entirely from real,
already-persisted rows (`UserBookmark`, `ApplicationTracker`,
`OpportunityChangeHistory`, `UserIntelligenceSnapshot`). Nothing here
calls an LLM, and nothing here claims progress the underlying data
doesn't support -- Part J's own explicit "do not claim 'you are ready
for X' unless the data supports that conclusion" rule is enforced by
construction: every guidance message is a template filled with a real
count/date/title, never free text.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_tracker import ApplicationTracker
from app.models.enums import BookmarkStatus, NextActionType, TrackerStatus
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.models.user import User
from app.services.user_intelligence import UserIntelligenceSnapshot, build_user_intelligence_snapshot

# Application-tracker statuses that represent real, unfinished work --
# deliberately excludes the three terminal states (APPLIED, ACCEPTED,
# REJECTED), each of which means there is no further action to prompt.
_INCOMPLETE_TRACKER_STATUSES = frozenset(
    {TrackerStatus.INTERESTED.value, TrackerStatus.PREPARING_DOCS.value, TrackerStatus.INTERVIEWING.value}
)

DEADLINE_GUIDANCE_WINDOW_DAYS = 14
RECENT_CHANGE_WINDOW_DAYS = 7


@dataclasses.dataclass
class NextActionItem:
    action_type: str
    label: str
    reason: str
    opportunity_id: uuid.UUID | None = None


@dataclasses.dataclass
class GuidanceItem:
    category: str
    message: str
    next_actions: list[NextActionItem]


@dataclasses.dataclass
class GuidanceResult:
    items: list[GuidanceItem]
    next_actions: list[NextActionItem]
    generated_at: dt.datetime


async def _upcoming_deadline_guidance(db: AsyncSession, *, user: User, now: dt.datetime) -> GuidanceItem | None:
    upcoming = []
    for bookmark in user.bookmarks:
        if bookmark.opportunity is None or bookmark.opportunity.deadline is None:
            continue
        deadline = bookmark.opportunity.deadline
        deadline = deadline if deadline.tzinfo else deadline.replace(tzinfo=dt.timezone.utc)
        days_remaining = (deadline - now).days
        if 0 <= days_remaining <= DEADLINE_GUIDANCE_WINDOW_DAYS:
            upcoming.append((bookmark.opportunity, days_remaining))

    if not upcoming:
        return None

    upcoming.sort(key=lambda pair: pair[1])
    actions = [
        NextActionItem(
            action_type=NextActionType.REVIEW_DEADLINE.value,
            label=f"Review deadline for {opp.title}",
            reason=f"Deadline in {days} day(s).",
            opportunity_id=opp.id,
        )
        for opp, days in upcoming
    ]
    return GuidanceItem(
        category="upcoming_deadlines",
        message=f"You have {len(upcoming)} saved opportunity(ies) with a deadline in the next {DEADLINE_GUIDANCE_WINDOW_DAYS} days.",
        next_actions=actions,
    )


async def _changed_deadline_guidance(db: AsyncSession, *, user: User, now: dt.datetime) -> GuidanceItem | None:
    """Part M's own "opportunity change -> user impact" pattern, read
    directly at guidance-generation time (rather than requiring a
    separate notification to have already fired) -- reuses the existing,
    real `OpportunityChangeHistory` table, no new state."""
    bookmarked_ids = {b.opportunity_id for b in user.bookmarks if b.opportunity_id is not None}
    if not bookmarked_ids:
        return None

    cutoff = now - dt.timedelta(days=RECENT_CHANGE_WINDOW_DAYS)
    result = await db.execute(
        select(OpportunityChangeHistory).where(
            OpportunityChangeHistory.opportunity_id.in_(bookmarked_ids),
            OpportunityChangeHistory.field_name == "deadline",
            OpportunityChangeHistory.changed_at >= cutoff,
        )
    )
    changes = list(result.scalars().all())
    if not changes:
        return None

    opp_by_id = {b.opportunity_id: b.opportunity for b in user.bookmarks if b.opportunity_id is not None}
    actions = [
        NextActionItem(
            action_type=NextActionType.REVIEW_DEADLINE.value,
            label=f"Review the new deadline for {opp_by_id[c.opportunity_id].title if opp_by_id.get(c.opportunity_id) else 'a saved opportunity'}",
            reason=f"Deadline changed from {c.old_value} to {c.new_value}.",
            opportunity_id=c.opportunity_id,
        )
        for c in changes
    ]
    return GuidanceItem(
        category="saved_opportunity_deadline_changed",
        message=f"The deadline changed for {len(changes)} of your saved opportunity(ies).",
        next_actions=actions,
    )


def _incomplete_application_guidance(*, trackers: list[ApplicationTracker]) -> GuidanceItem | None:
    incomplete = [t for t in trackers if t.status in _INCOMPLETE_TRACKER_STATUSES]
    if not incomplete:
        return None
    actions = [
        NextActionItem(
            action_type=NextActionType.COMPLETE_APPLICATION_STEP.value,
            label=f"Continue your application to {t.scholarship_name}",
            reason=f"Currently at status '{t.status}'.",
        )
        for t in incomplete
    ]
    return GuidanceItem(
        category="incomplete_applications",
        message=f"You have {len(incomplete)} application(s) in progress that still need action.",
        next_actions=actions,
    )


def _profile_completion_guidance(snapshot: UserIntelligenceSnapshot) -> GuidanceItem | None:
    """Part 3 of this phase's own required worked example: a user has a
    goal but insufficient profile information -- guide them toward
    completing ONLY the fields that materially improve matching (Part
    A's own "avoid profile bloat"), never toward filling out the whole
    profile indiscriminately."""
    if not snapshot.opportunity_gaps:
        return None

    has_goal = bool(snapshot.goals_and_interests.application_goals)
    actions = [
        NextActionItem(
            action_type=(
                NextActionType.REVIEW_GOAL.value
                if has_goal and gap.field in {"interests", "application_goals"}
                else NextActionType.UPDATE_PROFILE.value
            ),
            label=f"Add your {gap.field.replace('_', ' ')}",
            reason=gap.reason,
        )
        for gap in snapshot.opportunity_gaps
    ]
    return GuidanceItem(
        category="profile_completion_relevant_to_matching",
        message=(
            f"Completing {len(snapshot.opportunity_gaps)} field(s) on your profile would materially "
            "improve how accurately APEX can match you to opportunities."
        ),
        next_actions=actions,
    )


async def build_guidance(db: AsyncSession, *, user: User, now: dt.datetime | None = None) -> GuidanceResult:
    now = now or dt.datetime.now(dt.timezone.utc)

    snapshot = await build_user_intelligence_snapshot(db, user=user, now=now)

    tracker_result = await db.execute(select(ApplicationTracker).where(ApplicationTracker.user_id == user.id))
    trackers = list(tracker_result.scalars().all())

    candidate_items = [
        await _upcoming_deadline_guidance(db, user=user, now=now),
        await _changed_deadline_guidance(db, user=user, now=now),
        _incomplete_application_guidance(trackers=trackers),
        _profile_completion_guidance(snapshot),
    ]
    items = [item for item in candidate_items if item is not None]

    all_actions = [action for item in items for action in item.next_actions]

    return GuidanceResult(items=items, next_actions=all_actions, generated_at=now)
