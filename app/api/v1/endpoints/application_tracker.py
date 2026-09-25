import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.application_tracker import ApplicationTracker
from app.schemas.application_intelligence import (
    ApplicationIntelligenceSnapshotOut,
    ChecklistTaskOut,
    OpportunityChangeOut,
    RequirementOut,
    RequirementsOut,
)
from app.schemas.application_tracker import (
    TodoToggleUpdate,
    TrackerCreate,
    TrackerOut,
    TrackerStatusUpdate,
)
from app.services import application_intelligence
from app.services import application_tracker as tracker_service
from app.services.application_state_machine import InvalidTransitionError

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tracker", tags=["application-tracker"])


@router.post("/", response_model=TrackerOut, status_code=status.HTTP_201_CREATED)
async def create_tracker(
    payload: TrackerCreate, current_user: CurrentUser, db: DbSession
) -> ApplicationTracker:
    """Application Intelligence step, Part A/K: `payload.opportunity_id`
    (new, optional) links this tracker to a real catalog opportunity --
    see `tracker_service.create_tracker`'s own docstring for the full
    prefill/duplicate-protection behavior."""
    try:
        return await tracker_service.create_tracker(
            db,
            current_user.id,
            payload.scholarship_name,
            payload.institution,
            payload.deadline,
            payload.opportunity_id,
        )
    except tracker_service.TodoGenerationError as exc:
        logger.exception(
            "To-do list generation failed for scholarship %r", payload.scholarship_name
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate preparation checklist: {exc}",
        ) from exc
    except tracker_service.OpportunityNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found"
        ) from exc
    except tracker_service.DuplicateApplicationError as exc:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="You already have an application tracker linked to this opportunity",
        ) from exc


@router.get("/", response_model=list[TrackerOut])
async def list_trackers(current_user: CurrentUser, db: DbSession) -> list[ApplicationTracker]:
    return await tracker_service.get_user_trackers(db, current_user.id)


@router.get("/{tracker_id}", response_model=TrackerOut)
async def get_tracker(tracker_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> ApplicationTracker:
    """Application Intelligence step, Part K: a genuinely missing
    single-tracker read (Part 0's own audit found only list/create/
    status/todo/delete existed) -- same IDOR-safe ownership lookup as
    every other route in this router."""
    try:
        return await tracker_service.get_owned_tracker(db, current_user.id, tracker_id)
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc


@router.patch("/{tracker_id}/status", response_model=TrackerOut)
async def update_tracker_status(
    tracker_id: uuid.UUID,
    payload: TrackerStatusUpdate,
    current_user: CurrentUser,
    db: DbSession,
) -> ApplicationTracker:
    """Application Intelligence step, Part B/U: every transition now
    passes through a real, deterministic transition table
    (`application_state_machine.validate_generic_transition`) --
    `new_status="applied"` is REJECTED here (422) by design; submission
    has its own dedicated, explicit pathway (`POST /{tracker_id}/submit`,
    Part M) that a generic status PATCH cannot bypass."""
    try:
        return await tracker_service.update_tracker_status(
            db, current_user.id, tracker_id, payload.new_status
        )
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Invalid status transition: {exc.reason}",
        ) from exc


@router.post("/{tracker_id}/submit", response_model=TrackerOut)
async def submit_tracker(tracker_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> ApplicationTracker:
    """Application Intelligence step, Part K/M: the ONE controlled
    submission pathway. Means exactly "the user explicitly told APEX
    they submitted this application" -- APEX has no real integration
    with any external application system, so this never claims to have
    submitted anything on the user's behalf (see
    `tracker_service.submit_tracker`'s own docstring)."""
    try:
        return await tracker_service.submit_tracker(db, current_user.id, tracker_id)
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc
    except InvalidTransitionError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"Cannot submit this application: {exc.reason}",
        ) from exc


@router.get("/{tracker_id}/requirements", response_model=RequirementsOut)
async def get_tracker_requirements(
    tracker_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> RequirementsOut:
    """Application Intelligence step, Part C/D: the deterministic,
    evidence-traceable requirements projection for this tracker's linked
    opportunity (empty list, never fabricated, when the tracker isn't
    linked to a catalog opportunity)."""
    try:
        tracker = await tracker_service.get_owned_tracker(db, current_user.id, tracker_id)
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc

    snapshot = await application_intelligence.build_application_intelligence_snapshot(db, tracker=tracker)
    return RequirementsOut(
        tracker_id=tracker.id,
        opportunity_id=tracker.opportunity_id,
        requirements=[
            RequirementOut(
                key=r.key, label=r.label, status=r.status, source_field=r.source_field,
                evidence_snippet=r.evidence_snippet, completion=r.completion, completed_at=r.completed_at,
            )
            for r in snapshot.requirements
        ],
    )


@router.post("/{tracker_id}/requirements/{requirement_key}/complete", response_model=RequirementsOut)
async def complete_tracker_requirement(
    tracker_id: uuid.UUID, requirement_key: str, current_user: CurrentUser, db: DbSession
) -> RequirementsOut:
    """Application Intelligence step, Part E: explicit
    USER_CONFIRMED_COMPLETE -- never inferred from a document upload or
    any other side effect (Part N's own explicit warning)."""
    try:
        await tracker_service.complete_requirement(db, current_user.id, tracker_id, requirement_key)
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc
    return await get_tracker_requirements(tracker_id, current_user, db)


@router.post("/{tracker_id}/requirements/{requirement_key}/uncomplete", response_model=RequirementsOut)
async def uncomplete_tracker_requirement(
    tracker_id: uuid.UUID, requirement_key: str, current_user: CurrentUser, db: DbSession
) -> RequirementsOut:
    try:
        await tracker_service.uncomplete_requirement(db, current_user.id, tracker_id, requirement_key)
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc
    return await get_tracker_requirements(tracker_id, current_user, db)


@router.get("/{tracker_id}/intelligence", response_model=ApplicationIntelligenceSnapshotOut)
async def get_tracker_intelligence(
    tracker_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> ApplicationIntelligenceSnapshotOut:
    """Application Intelligence step, Part J: the read-oriented snapshot
    (status, linked opportunity identity, deadline state, requirements,
    AI checklist, recent opportunity changes, submission state) -- a
    projection, never a second application model."""
    try:
        tracker = await tracker_service.get_owned_tracker(db, current_user.id, tracker_id)
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc

    snapshot = await application_intelligence.build_application_intelligence_snapshot(db, tracker=tracker)
    return ApplicationIntelligenceSnapshotOut(
        tracker_id=snapshot.tracker_id,
        status=snapshot.status,
        scholarship_name=snapshot.scholarship_name,
        institution=snapshot.institution,
        opportunity_id=snapshot.opportunity_id,
        opportunity_title=snapshot.opportunity_title,
        deadline=snapshot.deadline,
        deadline_state=snapshot.deadline_state,
        requirements=[
            RequirementOut(
                key=r.key, label=r.label, status=r.status, source_field=r.source_field,
                evidence_snippet=r.evidence_snippet, completion=r.completion, completed_at=r.completed_at,
            )
            for r in snapshot.requirements
        ],
        ai_checklist=[
            ChecklistTaskOut(task_id=t.task_id, task_name=t.task_name, is_completed=t.is_completed)
            for t in snapshot.ai_checklist
        ],
        recent_opportunity_changes=[
            OpportunityChangeOut(
                field_name=c.field_name, old_value=c.old_value, new_value=c.new_value, changed_at=c.changed_at,
            )
            for c in snapshot.recent_opportunity_changes
        ],
        is_submitted=snapshot.is_submitted,
        submitted_at=snapshot.submitted_at,
        generated_at=snapshot.generated_at,
    )


@router.patch("/{tracker_id}/todo/{task_id}", response_model=TrackerOut)
async def toggle_todo_task(
    tracker_id: uuid.UUID,
    task_id: int,
    payload: TodoToggleUpdate,
    current_user: CurrentUser,
    db: DbSession,
) -> ApplicationTracker:
    try:
        return await tracker_service.toggle_todo_task(
            db, current_user.id, tracker_id, task_id, payload.is_completed
        )
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc
    except tracker_service.TodoTaskNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Task not found") from exc


@router.delete("/{tracker_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_tracker(tracker_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> None:
    try:
        await tracker_service.delete_tracker(db, current_user.id, tracker_id)
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc
