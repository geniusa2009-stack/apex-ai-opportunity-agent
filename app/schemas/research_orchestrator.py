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
    escalation_unavailable: bool = False
    attempts: list[dict] = []
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


class DiscoveryTraceOut(BaseModel):
    """Real, persisted adaptive-discovery execution trace for one
    aggregator run (Deliverable 4.2) -- read back from the
    trajectory's own `trajectory_metadata["discovery_summary"]` (see
    `app/workers/aggregator_tasks.py::_run_adaptive_discovery_rounds`).
    At minimum inspectable, per this deliverable's own spec: discovery
    depth, number of rounds, number of searches (sources polled),
    candidate yield, duplicate rate, stop/escalation outcome, provider
    used, trajectory ID."""

    trajectory_id: str
    objective: str
    discovery_execution: str
    initial_depth: str
    final_depth: str
    complexity_class: str
    complexity_score: int
    graph_related_count: int | None
    total_rounds: int
    total_sources_configured: int
    total_sources_polled: int
    escalation_count: int
    stop_reason: str
    provider: str
    provider_task: str
    processed: int
    created: int
    updated: int
    errors: int
    rounds: list[dict]


class ProviderRegistryEntryOut(BaseModel):
    """Deliverable 5, Feature 5 (Real Provider Escalation Validation):
    the live, real capability/availability state of one configured
    provider/model pairing (`research_orchestrator.PROVIDER_CAPABILITIES`),
    surfaced over the admin API so escalation-readiness is real and
    observable rather than silent. `available` is computed live from
    whether that provider's real API key setting is a genuinely
    non-empty string -- never fabricated (see
    `research_orchestrator._provider_available`)."""

    provider: str
    model: str
    task_suitability: list[str]
    reasoning_strength: str
    priority: int
    available: bool
