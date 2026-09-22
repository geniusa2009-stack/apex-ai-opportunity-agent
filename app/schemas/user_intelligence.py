"""APEX User Intelligence + Proactive Guidance step: response shapes for
`GET /users/me/intelligence`, `GET /users/me/feed`, `GET /users/me/guidance`,
`GET /users/me/activity`, and `POST /opportunities/{id}/feedback`.

Built directly from the real dataclasses in `app/services/user_intelligence.py`
/ `app/services/opportunity_feed.py` / `app/services/guidance_service.py` --
these schemas are a public API shape, not a second copy of that logic (same
posture as `app/schemas/personalization.py` from the immediately prior
phase). Part I/P: no internal ranking features, no sensitive attributes, no
trust/confidence scores are exposed anywhere in this file.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel

from app.schemas.personalization import EligibilityOut, MatchSignalsOut


class ProfileFactsOut(BaseModel):
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


class GoalsAndInterestsOut(BaseModel):
    application_goals: list[str]
    interests: list[str]


class PreferencesOut(BaseModel):
    target_country: str | None
    preferred_opportunity_types: list[str]
    remote_preference: bool | None
    funding_preference: str | None
    deadline_lead_time_days: int | None


class CurrentJourneyOut(BaseModel):
    saved_not_yet_applied_count: int
    applied_count: int
    active_application_tracker_count: int
    tracker_status_counts: dict[str, int]


class ActivityItemOut(BaseModel):
    kind: str
    opportunity_id: uuid.UUID | None
    opportunity_title: str | None
    occurred_at: dt.datetime
    weight: float


class ProfileGapOut(BaseModel):
    field: str
    reason: str


class UserIntelligenceSnapshotOut(BaseModel):
    user_id: uuid.UUID
    profile: ProfileFactsOut
    goals_and_interests: GoalsAndInterestsOut
    preferences: PreferencesOut
    current_journey: CurrentJourneyOut
    recent_activity: list[ActivityItemOut]
    opportunity_gaps: list[ProfileGapOut]
    generated_at: dt.datetime


class ActivityOut(BaseModel):
    items: list[ActivityItemOut]


class FeedItemOut(BaseModel):
    opportunity_id: uuid.UUID
    title: str
    opportunity_type: str
    deadline: dt.datetime | None
    lifecycle_state: str
    eligibility: EligibilityOut
    signals: MatchSignalsOut
    personalization_score: float
    reasons: list[str]
    feed_reasons: list[str]
    missing_information: list[str]
    is_exploration: bool


class PersonalFeedOut(BaseModel):
    items: list[FeedItemOut]
    exploration_ratio: float
    candidate_pool_size: int
    excluded_dismissed_count: int


class NextActionItemOut(BaseModel):
    action_type: str
    label: str
    reason: str
    opportunity_id: uuid.UUID | None


class GuidanceItemOut(BaseModel):
    category: str
    message: str
    next_actions: list[NextActionItemOut]


class GuidanceOut(BaseModel):
    items: list[GuidanceItemOut]
    next_actions: list[NextActionItemOut]
    generated_at: dt.datetime


class OpportunityFeedbackIn(BaseModel):
    """Part O's four feedback phrases, as a closed vocabulary -- not
    free text. `feedback_type` is validated against this literal set at
    the Pydantic layer (same posture as `TrackerStatusUpdate`)."""

    feedback_type: str  # one of: not_interested | not_relevant | already_applied | more_like_this


class OpportunityFeedbackOut(BaseModel):
    feedback_type: str
    recorded: bool
