"""APEX User Intelligence + Proactive Guidance step, Part A/E: the
read-oriented `UserIntelligenceSnapshot` projection.

NOT A SECOND USER MODEL (Part E's own explicit rule, mirroring Part E of
the immediately preceding phase's Opportunity Passport): nothing here is
persisted. `build_user_intelligence_snapshot` reads already-persisted
`User`/`UserBookmark`/`ApplicationTracker`/`OpportunityInteraction` rows
and assembles a plain dataclass, computed fresh on every call --
deterministic and reproducible given the same underlying rows.

The four signal categories Part A's own instruction distinguishes are
kept structurally separate in this dataclass, never silently mixed:

    A) USER-PROVIDED FACTS        -> `profile` (identity/education fields
                                      the user typed into their own
                                      profile form)
    B) USER-EXPLICIT PREFERENCES  -> `preferences` (target_country,
                                      funding_preference, etc.)
    C) OBSERVED PRODUCT BEHAVIOR  -> `behavioral_signals`/
                                      `recent_activity` (bookmarks +
                                      `OpportunityInteraction` rows)
    D) DERIVED NON-SENSITIVE      -> `opportunity_gaps`/
       SIGNALS                      `profile_completion_relevant_to_
                                      matching` (computed, never a raw
                                      fact the user stated)
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.application_tracker import ApplicationTracker
from app.models.bookmark import UserBookmark
from app.models.enums import BookmarkStatus
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services.interaction_service import BEHAVIOR_SIGNAL_WEIGHT, get_recent_interactions

# Part A/D's own "add only genuinely missing high-value fields, avoid
# profile bloat" instruction, mirrored here: only fields that materially
# change either a hard-eligibility result (age, academic_level,
# residency/nationality) or a real personalization signal (interests,
# application_goals, target_country) are ever surfaced as a "gap" --
# skills/languages/gpa/graduation_year are real profile fields but do
# not currently feed any eligibility rule or match signal
# (`eligibility_engine.py`/`personalized_ranking.py`), so asking the
# user to fill them here would be guidance the system cannot yet act on
# (Part J's own "never invent user progress" / never promise value that
# isn't real).
_MATERIAL_PROFILE_GAPS: tuple[tuple[str, str], ...] = (
    ("age", "Needed to evaluate age-restricted opportunities accurately."),
    ("academic_level", "Needed to evaluate academic-level-restricted opportunities accurately."),
    ("residency_country", "Needed to evaluate country/residency-restricted opportunities accurately."),
    ("interests", "Improves how closely your feed matches what you actually care about."),
    ("application_goals", "Improves how closely your feed matches your stated goals."),
    ("target_country", "Improves geography-based personalization for opportunities you'd actually consider."),
)


@dataclasses.dataclass
class ProfileFacts:
    full_name: str | None
    age: int | None
    academic_level: str | None
    degree_level: str | None
    major: str | None
    gpa: float | None
    graduation_year: int | None
    nationality: str | None
    residency_country: str | None
    languages: list[str]
    skills: list[str]


@dataclasses.dataclass
class GoalsAndInterests:
    application_goals: list[str]
    interests: list[str]


@dataclasses.dataclass
class Preferences:
    target_country: str | None
    preferred_opportunity_types: list[str]
    remote_preference: bool | None
    funding_preference: str | None
    deadline_lead_time_days: int | None


@dataclasses.dataclass
class ActivityItem:
    kind: str  # "saved" | "applied" | one of InteractionEventType's values
    opportunity_id: uuid.UUID
    opportunity_title: str | None
    occurred_at: dt.datetime
    weight: float


@dataclasses.dataclass
class CurrentJourney:
    saved_not_yet_applied_count: int
    applied_count: int
    active_application_tracker_count: int
    tracker_status_counts: dict[str, int]


@dataclasses.dataclass
class ProfileGap:
    field: str
    reason: str


@dataclasses.dataclass
class UserIntelligenceSnapshot:
    user_id: uuid.UUID
    profile: ProfileFacts
    goals_and_interests: GoalsAndInterests
    preferences: Preferences
    current_journey: CurrentJourney
    recent_activity: list[ActivityItem]
    opportunity_gaps: list[ProfileGap]
    generated_at: dt.datetime


async def load_recent_activity(db: AsyncSession, *, user: User, limit: int = 10) -> list[ActivityItem]:
    """Merges `UserBookmark` (SAVED/APPLIED) and `OpportunityInteraction`
    (everything else) into ONE reproducible timeline, newest first --
    see `interaction_service.py`'s own docstring for why these two
    sources are kept as separate tables but merged only at read time."""
    items: list[ActivityItem] = []

    for bookmark in user.bookmarks:
        if bookmark.opportunity_id is None:
            continue
        kind = "applied" if bookmark.status == BookmarkStatus.APPLIED else "saved"
        items.append(
            ActivityItem(
                kind=kind,
                opportunity_id=bookmark.opportunity_id,
                opportunity_title=bookmark.opportunity.title if bookmark.opportunity else None,
                occurred_at=bookmark.created_at,
                weight=BEHAVIOR_SIGNAL_WEIGHT.get(kind, 0.0),
            )
        )

    interactions = await get_recent_interactions(db, user_id=user.id, limit=limit)
    if interactions:
        opp_ids = {i.opportunity_id for i in interactions}
        titles_result = await db.execute(select(Opportunity.id, Opportunity.title).where(Opportunity.id.in_(opp_ids)))
        titles = dict(titles_result.all())
        for interaction in interactions:
            items.append(
                ActivityItem(
                    kind=interaction.event_type,
                    opportunity_id=interaction.opportunity_id,
                    opportunity_title=titles.get(interaction.opportunity_id),
                    occurred_at=interaction.created_at,
                    weight=BEHAVIOR_SIGNAL_WEIGHT.get(interaction.event_type, 0.0),
                )
            )

    items.sort(key=lambda i: i.occurred_at, reverse=True)
    return items[:limit]


def _compute_opportunity_gaps(user: User) -> list[ProfileGap]:
    gaps: list[ProfileGap] = []
    for field, reason in _MATERIAL_PROFILE_GAPS:
        value = getattr(user, field)
        if value is None or (isinstance(value, list) and len(value) == 0):
            gaps.append(ProfileGap(field=field, reason=reason))
    return gaps


async def build_user_intelligence_snapshot(
    db: AsyncSession, *, user: User, now: dt.datetime | None = None
) -> UserIntelligenceSnapshot:
    now = now or dt.datetime.now(dt.timezone.utc)

    saved_not_yet_applied = sum(
        1 for b in user.bookmarks if b.opportunity_id is not None and b.status == BookmarkStatus.SAVED
    )
    applied_count = sum(
        1 for b in user.bookmarks if b.opportunity_id is not None and b.status == BookmarkStatus.APPLIED
    )

    tracker_result = await db.execute(select(ApplicationTracker).where(ApplicationTracker.user_id == user.id))
    trackers = list(tracker_result.scalars().all())
    tracker_status_counts: dict[str, int] = {}
    for tracker in trackers:
        tracker_status_counts[tracker.status] = tracker_status_counts.get(tracker.status, 0) + 1

    return UserIntelligenceSnapshot(
        user_id=user.id,
        profile=ProfileFacts(
            full_name=user.full_name,
            age=user.age,
            academic_level=user.academic_level.value if user.academic_level else None,
            degree_level=user.degree_level,
            major=user.major,
            gpa=user.gpa,
            graduation_year=user.graduation_year,
            nationality=user.nationality,
            residency_country=user.residency_country,
            languages=list(user.languages or []),
            skills=list(user.skills or []),
        ),
        goals_and_interests=GoalsAndInterests(
            application_goals=list(user.application_goals or []),
            interests=list(user.interests or []),
        ),
        preferences=Preferences(
            target_country=user.target_country,
            preferred_opportunity_types=[t.value for t in (user.preferred_opportunity_types or [])],
            remote_preference=user.remote_preference,
            funding_preference=user.funding_preference,
            deadline_lead_time_days=user.deadline_lead_time_days,
        ),
        current_journey=CurrentJourney(
            saved_not_yet_applied_count=saved_not_yet_applied,
            applied_count=applied_count,
            active_application_tracker_count=len(trackers),
            tracker_status_counts=tracker_status_counts,
        ),
        recent_activity=await load_recent_activity(db, user=user),
        opportunity_gaps=_compute_opportunity_gaps(user),
        generated_at=now,
    )
