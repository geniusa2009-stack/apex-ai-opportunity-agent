"""Search Trajectory Memory admin endpoints (Deliverable 3).

Moderator+ tier -- same posture as `admin_opportunities.py`'s coverage
endpoint (Deliverable 1): this is internal research-planning tooling,
not public browsing, but doesn't need Senior-Moderator-only either since
it's read-only and touches no real production case data.
"""
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import select

from app.api.deps import DbSession, RequireModerationAccess
from app.models.research_trajectory import ResearchTrajectory, ResearchTrajectoryStep
from app.schemas.research_orchestrator import (
    DiscoveryTraceOut,
    ProviderPerformanceOut,
    ProviderRegistryEntryOut,
    RoutingDecisionOut,
)
from app.schemas.research_trajectory import (
    ResearchHintsOut,
    ResearchTrajectoryOut,
    ResearchTrajectoryStepOut,
    SourcePerformanceOut,
)
from app.services import research_orchestrator, research_trajectory_memory

router = APIRouter(prefix="/admin/research", tags=["admin-research"])


@router.get("/trajectories/{trajectory_id}", response_model=ResearchTrajectoryOut)
async def get_trajectory(
    trajectory_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
) -> ResearchTrajectoryOut:
    """Real trajectory plus its real, ordered steps."""
    trajectory = await db.get(ResearchTrajectory, trajectory_id)
    if trajectory is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Trajectory not found")

    result = await db.execute(
        select(ResearchTrajectoryStep)
        .where(ResearchTrajectoryStep.trajectory_id == trajectory_id)
        .order_by(ResearchTrajectoryStep.step_number.asc())
    )
    steps = result.scalars().all()

    out = ResearchTrajectoryOut.model_validate(trajectory)
    # `ResearchTrajectoryOut.model_validate` above can't see the
    # separately-queried `steps` list (no ORM relationship wired
    # between the two models -- deliberate, see this deliverable's own
    # "don't duplicate huge page contents" posture in
    # `research_trajectory.py`'s module docstring) -- set it explicitly.
    out.steps = [ResearchTrajectoryStepOut.model_validate(s) for s in steps]
    return out


@router.get("/hints", response_model=ResearchHintsOut)
async def get_research_hints(
    db: DbSession,
    _: RequireModerationAccess,
    objective: str = Query(min_length=1, max_length=500),
) -> ResearchHintsOut:
    """THE important function (Deliverable 3, Step 3), exposed for a
    real research planner to call before starting a new run. Real
    hints built entirely from real, past trajectory data -- see
    `research_trajectory_memory.build_research_hints`'s own docstring
    for the full "hint, not proof" reasoning."""
    hints = await research_trajectory_memory.build_research_hints(db, objective=objective)
    return ResearchHintsOut(**hints)


@router.get("/source-performance", response_model=list[SourcePerformanceOut])
async def get_source_performance(
    db: DbSession,
    _: RequireModerationAccess,
    limit: int = Query(default=20, ge=1, le=100),
) -> list[SourcePerformanceOut]:
    """Real, grouped-by-domain counts -- deliberately never a trust/truth
    verdict. See `research_trajectory_memory.get_source_performance`'s
    own docstring (Step 7, poisoning resistance)."""
    performance = await research_trajectory_memory.get_source_performance(db, limit=limit)
    return [SourcePerformanceOut(**p) for p in performance]


# --- Adaptive Research Orchestrator & Model Routing additions (Deliverable 4) ---


@router.get("/routing/{trajectory_id}", response_model=RoutingDecisionOut)
async def get_routing_trace(
    trajectory_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
) -> RoutingDecisionOut:
    """Real, persisted routing decision/trace for one trajectory --
    read back from its `route_and_verify` step's `step_metadata`
    (see `research_orchestrator.get_routing_trace`'s own docstring).
    404 both when the trajectory itself doesn't exist and when it
    exists but was never orchestrated by this deliverable (e.g. a plain
    D3 aggregator-sweep trajectory with no routing step) -- either way,
    there is no real routing decision to return."""
    trace = await research_orchestrator.get_routing_trace(db, trajectory_id)
    if trace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Routing trace not found")
    return RoutingDecisionOut(**trace)


# --- Adaptive Discovery Execution additions (Deliverable 4.2) ---


@router.get("/discovery/{trajectory_id}", response_model=DiscoveryTraceOut)
async def get_discovery_trace(
    trajectory_id: uuid.UUID,
    db: DbSession,
    _: RequireModerationAccess,
) -> DiscoveryTraceOut:
    """Real, persisted adaptive-discovery execution trace for one
    aggregator run -- see `research_orchestrator.get_discovery_trace`'s
    own docstring. 404 both when the trajectory doesn't exist and when
    it exists but carries no discovery summary (e.g. a `route_and_verify`
    -only trajectory from D4/D4.1)."""
    trace = await research_orchestrator.get_discovery_trace(db, trajectory_id)
    if trace is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Discovery trace not found")
    return DiscoveryTraceOut(**trace)


@router.get("/provider-performance", response_model=list[ProviderPerformanceOut])
async def get_provider_performance(
    db: DbSession,
    _: RequireModerationAccess,
) -> list[ProviderPerformanceOut]:
    """Real, grouped-by-provider routing-selection counts. Never a
    trust/truth verdict -- see `research_orchestrator.get_provider_performance`'s
    own docstring. No secrets/prompts in this response -- only provider
    names, real counts, and timestamps."""
    performance = await research_orchestrator.get_provider_performance(db)
    return [ProviderPerformanceOut(**p) for p in performance]



@router.get("/providers", response_model=list[ProviderRegistryEntryOut])
async def get_provider_registry(
    _: RequireModerationAccess,
) -> list[ProviderRegistryEntryOut]:
    """Deliverable 5, Feature 5: the live provider capability registry
    (`research_orchestrator.get_provider_registry`) -- which real
    providers this codebase has wired up, what each is suitable for, and
    whether its real API key is genuinely configured in THIS deployment
    right now. In this dev sandbox every provider's `available` is
    `False` (all four API key settings are empty strings) -- this
    endpoint surfaces that honestly rather than hiding it; it does not
    fabricate a second, "ready" provider."""
    registry = research_orchestrator.get_provider_registry()
    return [ProviderRegistryEntryOut(**entry) for entry in registry]
