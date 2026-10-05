"""API schemas for the APEX AI Opportunity Agent endpoints."""

from __future__ import annotations

import datetime as dt
import uuid
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from app.agent.performance import PerformanceInput


class VolunteerSubmission(BaseModel):
    text: str = Field(min_length=10, max_length=20_000, description="The opportunity as the volunteer found it")
    links: list[str] = Field(default_factory=list, max_length=25)
    note: str | None = Field(default=None, max_length=500, description="Where it came from (optional)")


class IngestResponse(BaseModel):
    status: str
    source_message_id: uuid.UUID | None = None
    job_id: uuid.UUID | None = None
    reason: str | None = None


class WebhookResponse(BaseModel):
    received: int
    accepted: int
    by_status: dict[str, int]


class JobEventOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    from_status: str | None
    to_status: str
    stage: str | None
    detail: dict[str, Any] | None
    duration_ms: int | None
    created_at: dt.datetime


class JobOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    job_type: str
    correlation_id: str
    status: str
    outcome: str | None
    priority: int
    source_message_id: uuid.UUID | None
    opportunity_id: uuid.UUID | None
    draft_id: uuid.UUID | None
    attempts: int
    max_attempts: int
    next_retry_at: dt.datetime | None
    last_error: str | None
    last_error_code: str | None
    last_error_category: str | None
    created_at: dt.datetime
    updated_at: dt.datetime
    completed_at: dt.datetime | None


class JobDetailOut(JobOut):
    payload: dict[str, Any] | None
    events: list[JobEventOut]
    message_excerpt: str | None
    message_links: list[str] | None


class DraftOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    opportunity_id: uuid.UUID
    job_id: uuid.UUID | None
    version: int
    body: str
    writer_mode: str
    status: str
    review_decision: str | None
    review_reasons: list[dict[str, Any]] | None
    fact_guard: dict[str, Any] | None
    created_at: dt.datetime


class ApprovalItemOut(BaseModel):
    draft: DraftOut
    opportunity_title: str
    verification: dict[str, Any] | None
    dedup_outcome: str | None
    duplicate_of: str | None
    update_proposal: dict[str, Any] | None
    source_trust_score: float | None


class ApproveRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)
    edited_body: str | None = Field(default=None, min_length=20, max_length=4000)


class RejectRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)
    inaccurate_source: bool = Field(default=False, description="The source shared wrong/misleading information")


class RevisionRequest(BaseModel):
    notes: str = Field(min_length=3, max_length=1000)


class JobDecisionRequest(BaseModel):
    notes: str | None = Field(default=None, max_length=1000)


class DiscardRequest(BaseModel):
    reason: str = Field(min_length=3, max_length=1000)


class DecisionResponse(BaseModel):
    success: bool
    job_status: str | None = None
    draft_status: str | None = None
    warnings: list[str] = Field(default_factory=list)
    updated_fields: list[str] = Field(default_factory=list)


class ConfirmPublicationRequest(BaseModel):
    external_url: str | None = Field(default=None, max_length=2048)
    external_id: str | None = Field(default=None, max_length=255)


class PublicationOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    draft_id: uuid.UUID
    opportunity_id: uuid.UUID
    platform: str
    target: str
    status: str
    attempts: int
    external_id: str | None
    external_url: str | None
    last_error: str | None
    published_at: dt.datetime | None


class PerformanceRequest(PerformanceInput):
    pass


class PerformanceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    publication_id: uuid.UUID
    views: int | None
    reactions: int | None
    comments: int | None
    shares: int | None
    clicks: int | None
    applications: int | None
    engagement_score: float | None
    measured_at: dt.datetime


class SourceOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    name: str
    source_type: str
    trust_score: float
    access_status: str
    active_status: bool


class SourceUpdateRequest(BaseModel):
    trust_score: float | None = Field(default=None, ge=0, le=1)
    access_status: str | None = Field(default=None, pattern="^(approved|pending|revoked)$")
    active_status: bool | None = None
