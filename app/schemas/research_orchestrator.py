"""Adaptive Research Orchestrator & Model Routing (Deliverable 4) schemas.

API-facing shapes only. The internal dataclasses this mirrors live in
`app/services/research_orchestrator.py` -- see that module's own
docstring for the full design.
"""
from datetime import datetime

from pydantic import BaseModel


class RoutingDecisionOut(BaseModel):
    """One real routing decision, as persisted into a `ResearchTrajectory`
    step's `step_metadata` JSONB (see `research_orchestrator.py`'s
    "Persistence" section) and re-shaped here for the admin API."""

    trajectory_id: str
    objective: str
    task_type: str
    complexity_class: str
    complexity_score: int
    complexity_reasons: list[str]
    depth: str
    primary_provider: str | None
    primary_model: str | None
    provider_available: bool
    routing_reasons: list[str]
    max_model_calls: int
    escalation_triggered: bool
    escalation_reasons: list[str]
    escalation_attempt: int
    fallback_used: bool
    fallback_reason: str | None
    stopped_early: bool
    stop_reason: str | None
    trajectory_signals: dict
    decided_at: datetime | None


class ProviderPerformanceOut(BaseModel):
    """Real, aggregated counts of how often each provider was selected
    as PRIMARY by the router, and how those routed runs resolved --
    grouped-by-provider counts only, same disclosed "counts/timestamps,
    never a trust verdict" posture as
    `research_trajectory_memory.get_source_performance`."""

    provider: str
    times_selected_primary: int
    times_escalated_from: int
    times_fallback_used: int
    last_selected_at: str | None
