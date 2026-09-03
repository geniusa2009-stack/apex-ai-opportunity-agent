"""Search Trajectory Memory schemas (Deliverable 3)."""
import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict


class ResearchTrajectoryStepOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    step_number: int
    action_type: str
    query: str | None
    source_domain: str | None
    reference_id: str | None
    parent_step_id: uuid.UUID | None
    outcome: str
    usefulness_score: float | None
    discovered_entities_count: int
    discovered_opportunities_count: int
    verification_impact: str | None
    error_reason: str | None
    created_at: datetime


class ResearchTrajectoryOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    objective: str
    normalized_objective: str
    started_at: datetime
    completed_at: datetime | None
    status: str
    total_steps: int
    successful_steps: int
    failed_steps: int
    opportunities_discovered: int
    opportunities_verified: int
    useful_sources: list[str] | None
    research_depth: int
    final_outcome: str | None
    steps: list[ResearchTrajectoryStepOut] = []


class ResearchHintsOut(BaseModel):
    """Mirrors Deliverable 3's own example JSON shape exactly."""

    successful_queries: list[str]
    failed_queries: list[str]
    useful_domains: list[str]
    low_value_domains: list[str]
    recommended_next_actions: list[str]
    known_patterns: list[dict]


class SourcePerformanceOut(BaseModel):
    source_domain: str
    total_steps_seen: int
    positive_signal_count: int
    negative_signal_count: int
    last_seen_at: str
