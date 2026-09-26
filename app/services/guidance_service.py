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

from app.models.application_requirement_completion import ApplicationRequirementCompletion
from app.models.application_tracker import ApplicationTracker
from app.models.enums import BookmarkStatus, NextActionType, TrackerStatus
from app.models.opportunity_change_history import OpportunityChangeHistory
from app.models.enums import ChangeSeverity
from app.models.user import User
from app.services import application_requirements
from app.services.application_state_machine import INCOMPLETE_TRACKER_STATUSES
from app.services.user_intelligence import UserIntelligenceSnapshot, build_user_intelligence_snapshot

# Application-tracker statuses that represent real, unfinished work --
# deliberately excludes the three terminal states (APPLIED, ACCEPTED,
# REJECTED), each of which means there is no further action to prompt.
# Opportunity Monitoring & Personal Impact Engine step: moved to
# `application_state_machine.INCOMPLETE_TRACKER_STATUSES` (imported
# above) so `personal_impact_service.py` can reuse the same definition
# without a circular import; kept as this same local name so every
# existing reference below is unchanged.
_INCOMPLETE_TRACKER_STATUSES = INCOMPLETE_TRACKER_STATUSES

DEADLINE_GUIDANCE_WINDOW_DAYS = 14
RECENT_CHANGE_WINDOW_DAYS = 7

# Opportunity Monitoring & Personal Impact Engine step, Part C's own
# severity gate applied at guidance-generation time: MATERIAL and above
# reach guidance (a pull surface); MINOR/IGNORE never do.
_MATERIAL_PLUS_SEVERITIES = frozenset({ChangeSeverity.MATERIAL, ChangeSeverity.HIGH, ChangeSeverity.CRITICAL})


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


async def _application_missing_requirements_guidance(
    db: AsyncSession, *, trackers: list[ApplicationTracker]
) -> GuidanceItem | None:
    """Application Intelligence step, Part F: surfaces real, deterministic,
    still-missing REQUIRED items for each active, opportunity-linked
    tracker -- reuses `application_requirements.extract_requirements`
    (Part C) and `ApplicationRequirementCompletion` (Part D/E), never a
    second requirements engine."""
    actions: list[NextActionItem] = []
    for tracker in trackers:
        if tracker.status not in _INCOMPLETE_TRACKER_STATUSES or tracker.opportunity is None:
            continue

        required = [
            r for r in application_requirements.extract_requirements(tracker.opportunity)
            if r.status == application_requirements.REQUIRED
        ]
        if not required:
            continue

        completions_result = await db.execute(
            select(ApplicationRequirementCompletion).where(
                ApplicationRequirementCompletion.tracker_id == tracker.id,
                ApplicationRequirementCompletion.is_completed.is_(True),
            )
        )
        completed_keys = {row.requirement_key for row in completions_result.scalars().all()}

        for req in required:
            if req.key in completed_keys:
                continue
            actions.append(
                NextActionItem(
                    action_type=NextActionType.REVIEW_REQUIREMENTS.value,
                    label=f"Add your {req.label} for {tracker.scholarship_name}",
                    reason=f"This opportunity's own listing mentions: \"{req.evidence_snippet}\"",
                    opportunity_id=tracker.opportunity_id,
                )
            )

    if not actions:
        return None
    return GuidanceItem(
        category="application_missing_requirements",
        message=f"You have {len(actions)} missing application requirement(s) across your in-progress applications.",
        next_actions=actions,
    )


async def _application_change_impact_guidance(
    db: AsyncSession, *, trackers: list[ApplicationTracker], now: dt.datetime
) -> GuidanceItem | None:
    """Application Intelligence step, Part I: OPPORTUNITY CHANGE -> real
    ACTIVE application impact. Same read-time pattern as
    `_changed_deadline_guidance` above, scoped to opportunity-linked,
    non-terminal `ApplicationTracker` rows instead of bookmarks -- an
    active application's own catalog opportunity changing its deadline is
    at least as material to that user as a merely-saved one."""
    active_linked = {
        t.opportunity_id: t
        for t in trackers
        if t.status in _INCOMPLETE_TRACKER_STATUSES and t.opportunity_id is not None
    }
    if not active_linked:
        return None

    cutoff = now - dt.timedelta(days=RECENT_CHANGE_WINDOW_DAYS)
    result = await db.execute(
        select(OpportunityChangeHistory).where(
            OpportunityChangeHistory.opportunity_id.in_(active_linked.keys()),
            OpportunityChangeHistory.field_name == "deadline",
            OpportunityChangeHistory.changed_at >= cutoff,
        )
    )
    changes = list(result.scalars().all())
    if not changes:
        return None

    actions = [
        NextActionItem(
            action_type=NextActionType.REVIEW_DEADLINE.value,
            label=f"Review the new deadline for your application to {active_linked[c.opportunity_id].scholarship_name}",
            reason=f"Deadline changed from {c.old_value} to {c.new_value}.",
            opportunity_id=c.opportunity_id,
        )
        for c in changes
    ]
    return GuidanceItem(
        category="active_application_deadline_changed",
        message=f"The deadline changed for {len(changes)} of your active application(s).",
        next_actions=actions,
    )


async def _saved_opportunity_material_change_guidance(
    db: AsyncSession, *, user: User, now: dt.datetime
) -> GuidanceItem | None:
    """Opportunity Monitoring & Personal Impact Engine step, Part K/L:
    the eligibility/funding/lifecycle-state generalization of
    `_changed_deadline_guidance` above (which stays deadline-only and
    unmodified, to avoid re-testing its existing behavior) -- surfaces
    any OTHER MATERIAL-or-above severity change (Part C's own severity
    gate: MINOR/IGNORE never reach guidance) recorded against a saved
    opportunity in the last `RECENT_CHANGE_WINDOW_DAYS` days."""
    bookmarked_ids = {b.opportunity_id for b in user.bookmarks if b.opportunity_id is not None}
    if not bookmarked_ids:
        return None

    cutoff = now - dt.timedelta(days=RECENT_CHANGE_WINDOW_DAYS)
    result = await db.execute(
        select(OpportunityChangeHistory).where(
            OpportunityChangeHistory.opportunity_id.in_(bookmarked_ids),
            OpportunityChangeHistory.field_name != "deadline",
            OpportunityChangeHistory.severity.in_([s.value for s in _MATERIAL_PLUS_SEVERITIES]),
            OpportunityChangeHistory.changed_at >= cutoff,
        )
    )
    changes = list(result.scalars().all())
    if not changes:
        return None

    opp_by_id = {b.opportunity_id: b.opportunity for b in user.bookmarks if b.opportunity_id is not None}
    actions = [
        NextActionItem(
            action_type=NextActionType.REVIEW_OPPORTUNITY_CHANGE.value,
            label=f"Review the updated {c.field_name.replace('_', ' ')} for {opp_by_id[c.opportunity_id].title if opp_by_id.get(c.opportunity_id) else 'a saved opportunity'}",
            reason=f"{c.field_name.replace('_', ' ').capitalize()} changed from \"{c.old_value}\" to \"{c.new_value}\".",
            opportunity_id=c.opportunity_id,
        )
        for c in changes
    ]
    return GuidanceItem(
        category="saved_opportunity_material_change",
        message=f"{len(changes)} of your saved opportunity(ies) had a material update recently.",
        next_actions=actions,
    )


async def _application_material_change_guidance(
    db: AsyncSession, *, trackers: list[ApplicationTracker], now: dt.datetime
) -> GuidanceItem | None:
    """Opportunity Monitoring & Personal Impact Engine step, Part I/L:
    the same generalization as the function above, scoped to active,
    opportunity-linked `ApplicationTracker` rows instead of bookmarks --
    Part I's own literal "eligibility requirement changed... review the
    updated requirements" worked example."""
    active_linked = {
        t.opportunity_id: t
        for t in trackers
        if t.status in _INCOMPLETE_TRACKER_STATUSES and t.opportunity_id is not None
    }
    if not active_linked:
        return None

    cutoff = now - dt.timedelta(days=RECENT_CHANGE_WINDOW_DAYS)
    result = await db.execute(
        select(OpportunityChangeHistory).where(
            OpportunityChangeHistory.opportunity_id.in_(active_linked.keys()),
            OpportunityChangeHistory.field_name != "deadline",
            OpportunityChangeHistory.severity.in_([s.value for s in _MATERIAL_PLUS_SEVERITIES]),
            OpportunityChangeHistory.changed_at >= cutoff,
        )
    )
    changes = list(result.scalars().all())
    if not changes:
        return None

    actions = [
        NextActionItem(
            action_type=NextActionType.REVIEW_OPPORTUNITY_CHANGE.value,
            label=f"Review the updated {c.field_name.replace('_', ' ')} for your application to {active_linked[c.opportunity_id].scholarship_name}",
            reason=f"{c.field_name.replace('_', ' ').capitalize()} changed from \"{c.old_value}\" to \"{c.new_value}\".",
            opportunity_id=c.opportunity_id,
        )
        for c in changes
    ]
    return GuidanceItem(
        category="active_application_material_change",
        message=f"{len(changes)} of your active application(s) had a material opportunity update recently.",
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
        await _saved_opportunity_material_change_guidance(db, user=user, now=now),
        _incomplete_application_guidance(trackers=trackers),
        await _application_missing_requirements_guidance(db, trackers=trackers),
        await _application_change_impact_guidance(db, trackers=trackers, now=now),
        await _application_material_change_guidance(db, trackers=trackers, now=now),
        _profile_completion_guidance(snapshot),
    ]
    items = [item for item in candidate_items if item is not None]

    all_actions = [action for item in items for action in item.next_actions]

    return GuidanceResult(items=items, next_actions=all_actions, generated_at=now)
