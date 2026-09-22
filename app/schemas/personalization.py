"""Personalized Opportunity Intelligence + Opportunity Passport step:
response shapes for `GET /recommendations` (Part N) and
`GET /opportunities/{id}/passport` (Part O).

Built directly from the real dataclasses in `app/services/
eligibility_engine.py` / `app/services/personalized_ranking.py` /
`app/services/opportunity_passport.py` -- these schemas are a public API
shape, not a second copy of that logic.
"""

from __future__ import annotations

import datetime as dt
import uuid

from pydantic import BaseModel


class EligibilityOut(BaseModel):
    status: str
    matched_rules: list[str]
    failed_rules: list[str]
    unknown_rules: list[str]
    missing_information: list[str]


class MatchSignalsOut(BaseModel):
    interest_match: str
    goal_match: str
    type_match: str
    geography_match: str
    funding_match: str
    deadline_match: str
    graph_relevance: str


class RecommendationItemOut(BaseModel):
    opportunity_id: uuid.UUID
    title: str
    opportunity_type: str
    deadline: dt.datetime | None
    lifecycle_state: str
    eligibility: EligibilityOut
    signals: MatchSignalsOut
    personalization_score: float
    reasons: list[str]
    missing_information: list[str]
    is_exploration: bool


class RecommendationsOut(BaseModel):
    items: list[RecommendationItemOut]
    exploration_ratio: float
    total_eligible_candidates: int


class PassportIdentityOut(BaseModel):
    id: uuid.UUID
    title: str
    provider_source_id: uuid.UUID
    canonical_url: str
    opportunity_type: str
    raw_opportunity_type_label: str | None


class PassportFundingOut(BaseModel):
    funding_details: str | None


class PassportTemporalOut(BaseModel):
    lifecycle_state: str
    lifecycle_state_reason: str | None
    deadline: dt.datetime | None
    last_verified: dt.datetime | None
    last_updated: dt.datetime
    recent_changes: list[dict]


class PassportTrustOut(BaseModel):
    verification_status: str
    verification_reason: str
    evidence_count: int
    source_count: int
    authoritative_source_count: int
    contradiction_count: int
    last_evidence_update: dt.datetime | None


class PassportProvenanceOut(BaseModel):
    source_ids: list[uuid.UUID]
    evidence_ids: list[uuid.UUID]
    claim_count: int


class OpportunityPassportOut(BaseModel):
    identity: PassportIdentityOut
    eligibility: EligibilityOut | None
    funding: PassportFundingOut
    temporal: PassportTemporalOut
    trust: PassportTrustOut
    provenance: PassportProvenanceOut
