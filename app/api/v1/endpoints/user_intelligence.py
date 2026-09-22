"""APEX User Intelligence + Proactive Guidance step: the actual API
surface (Part T's product contract) over the service layer built in
`app/services/user_intelligence.py` / `opportunity_feed.py` /
`guidance_service.py`.

Registered as a SEPARATE router object sharing the same `/users` prefix
as `app/api/v1/endpoints/users.py` (same precedent as `recommendations.py`
extending its own router rather than duplicating one) -- both routers are
included in `app/api/v1/router.py`; FastAPI has no issue with two
`APIRouter`s sharing a prefix as long as their own path segments don't
collide (`/me/intelligence`, `/me/feed`, `/me/guidance`, `/me/activity`
here vs. `/me`, `/search` in `users.py` -- no overlap).

Part P (IDOR): every route below reads ONLY `current_user` from the auth
dependency -- none accepts a `user_id`/`opportunity_owner_id` path or
query parameter of any kind, so there is no way for one user to request
another user's snapshot, feed, guidance, or activity.
"""

import datetime as dt

from fastapi import APIRouter, Query

from app.api.deps import CurrentUser, DbSession
from app.models.enums import OpportunityType
from app.schemas.personalization import EligibilityOut, MatchSignalsOut
from app.schemas.user_intelligence import (
    ActivityItemOut,
    ActivityOut,
    CurrentJourneyOut,
    FeedItemOut,
    GoalsAndInterestsOut,
    GuidanceItemOut,
    GuidanceOut,
    NextActionItemOut,
    PersonalFeedOut,
    PreferencesOut,
    ProfileFactsOut,
    ProfileGapOut,
    UserIntelligenceSnapshotOut,
)
from app.services.guidance_service import build_guidance
from app.services.opportunity_feed import build_personal_feed
from app.services.personalized_ranking import DEFAULT_EXPLORATION_RATIO
from app.services.user_intelligence import build_user_intelligence_snapshot, load_recent_activity

router = APIRouter(prefix="/users", tags=["users"])


def _snapshot_to_out(snapshot) -> UserIntelligenceSnapshotOut:
    return UserIntelligenceSnapshotOut(
        user_id=snapshot.user_id,
        profile=ProfileFactsOut(**dataclass_fields(snapshot.profile)),
        goals_and_interests=GoalsAndInterestsOut(**dataclass_fields(snapshot.goals_and_interests)),
        preferences=PreferencesOut(**dataclass_fields(snapshot.preferences)),
        current_journey=CurrentJourneyOut(**dataclass_fields(snapshot.current_journey)),
        recent_activity=[
            ActivityItemOut(**dataclass_fields(item)) for item in snapshot.recent_activity
        ],
        opportunity_gaps=[ProfileGapOut(**dataclass_fields(gap)) for gap in snapshot.opportunity_gaps],
        generated_at=snapshot.generated_at,
    )


def dataclass_fields(obj) -> dict:
    """Small local helper -- every dataclass in `user_intelligence.py`/
    `guidance_service.py`/`opportunity_feed.py` has field names that
    already match their `*Out` Pydantic counterpart 1:1 (both were
    designed together, same posture as `personalization.py`'s own
    dataclass-to-schema mapping in `recommendations.py`), so a plain
    `vars()`/`asdict`-style copy is correct and avoids a long, repetitive
    field-by-field transcription that would just be a second place for
    typos to hide."""
    import dataclasses

    return dataclasses.asdict(obj) if dataclasses.is_dataclass(obj) else vars(obj)


@router.get("/me/intelligence", response_model=UserIntelligenceSnapshotOut)
async def get_my_intelligence_snapshot(
    current_user: CurrentUser, db: DbSession
) -> UserIntelligenceSnapshotOut:
    """Part E: the read-oriented `UserIntelligenceSnapshot` projection for
    the CALLER's own account only (Part P)."""
    snapshot = await build_user_intelligence_snapshot(db, user=current_user)
    return _snapshot_to_out(snapshot)


@router.get("/me/feed", response_model=PersonalFeedOut)
async def get_my_personal_feed(
    current_user: CurrentUser,
    db: DbSession,
    opportunity_type: OpportunityType | None = Query(default=None, description="Filter candidates by type."),
    country: str | None = Query(default=None, description="Filter candidates by country (exact match)."),
    limit: int = Query(default=20, ge=1, le=100),
    exploration_ratio: float = Query(default=DEFAULT_EXPLORATION_RATIO, ge=0.0, le=1.0),
) -> PersonalFeedOut:
    """Part F/H: the Personal Opportunity Feed -- reuses `rank_candidates`
    (same engine as `GET /recommendations`), on top of dismissed-exclusion
    and deterministic diversity re-sequencing (see `opportunity_feed.py`).
    """
    feed = await build_personal_feed(
        db,
        user=current_user,
        limit=limit,
        exploration_ratio=exploration_ratio,
        opportunity_type=opportunity_type,
        country=country,
    )

    items = [
        FeedItemOut(
            opportunity_id=fi.ranked.opportunity.id,
            title=fi.ranked.opportunity.title,
            opportunity_type=fi.ranked.opportunity.type.value,
            deadline=fi.ranked.opportunity.deadline,
            lifecycle_state=fi.ranked.opportunity.lifecycle_state,
            eligibility=EligibilityOut(
                status=fi.ranked.eligibility.status.value,
                matched_rules=[m.explanation for m in fi.ranked.eligibility.matched_rules],
                failed_rules=[f.explanation for f in fi.ranked.eligibility.failed_rules],
                unknown_rules=[u.explanation for u in fi.ranked.eligibility.unknown_rules],
                missing_information=fi.ranked.eligibility.missing_information,
            ),
            signals=MatchSignalsOut(
                interest_match=fi.ranked.signals.interest_match.value,
                goal_match=fi.ranked.signals.goal_match.value,
                type_match=fi.ranked.signals.type_match.value,
                geography_match=fi.ranked.signals.geography_match.value,
                funding_match=fi.ranked.signals.funding_match.value,
                deadline_match=fi.ranked.signals.deadline_match.value,
                graph_relevance=fi.ranked.signals.graph_relevance.value,
            ),
            personalization_score=fi.ranked.personalization_score,
            reasons=fi.ranked.reasons,
            feed_reasons=fi.feed_reasons,
            missing_information=fi.ranked.missing_information,
            is_exploration=fi.ranked.is_exploration,
        )
        for fi in feed.items
    ]

    return PersonalFeedOut(
        items=items,
        exploration_ratio=feed.exploration_ratio,
        candidate_pool_size=feed.candidate_pool_size,
        excluded_dismissed_count=feed.excluded_dismissed_count,
    )


@router.get("/me/guidance", response_model=GuidanceOut)
async def get_my_guidance(current_user: CurrentUser, db: DbSession) -> GuidanceOut:
    """Part J/K: deterministic proactive guidance + next-best-actions.
    `next_actions` is embedded here rather than exposed as a separate
    `/users/me/next-actions` endpoint (Part T's own "if appropriate
    after auditing... no redundant endpoints" -- every action already
    belongs to exactly one guidance item, so a second endpoint would just
    be the same data flattened twice)."""
    result = await build_guidance(db, user=current_user)
    return GuidanceOut(
        items=[
            GuidanceItemOut(
                category=item.category,
                message=item.message,
                next_actions=[NextActionItemOut(**dataclass_fields(a)) for a in item.next_actions],
            )
            for item in result.items
        ],
        next_actions=[NextActionItemOut(**dataclass_fields(a)) for a in result.next_actions],
        generated_at=result.generated_at,
    )


@router.get("/me/activity", response_model=ActivityOut)
async def get_my_activity(
    current_user: CurrentUser, db: DbSession, limit: int = Query(default=20, ge=1, le=100)
) -> ActivityOut:
    """Part D: the caller's own merged saved/applied/interaction timeline.
    Calls `user_intelligence.load_recent_activity` directly (the SAME
    function `build_user_intelligence_snapshot` uses internally for its
    own `recent_activity` field, so there is exactly one place this merge
    logic lives) rather than going through the snapshot, because the
    snapshot's own `recent_activity` is capped at a fixed default of 10
    -- this dedicated endpoint needs to honor the caller's own `limit`."""
    activity = await load_recent_activity(db, user=current_user, limit=limit)
    items = [ActivityItemOut(**dataclass_fields(i)) for i in activity]
    return ActivityOut(items=items)
