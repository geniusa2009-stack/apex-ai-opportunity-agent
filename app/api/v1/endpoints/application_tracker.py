import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession
from app.models.application_tracker import ApplicationTracker
from app.schemas.application_tracker import (
    TodoToggleUpdate,
    TrackerCreate,
    TrackerOut,
    TrackerStatusUpdate,
)
from app.services import application_tracker as tracker_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/tracker", tags=["application-tracker"])


@router.post("/", response_model=TrackerOut, status_code=status.HTTP_201_CREATED)
async def create_tracker(
    payload: TrackerCreate, current_user: CurrentUser, db: DbSession
) -> ApplicationTracker:
    try:
        return await tracker_service.create_tracker(
            db,
            current_user.id,
            payload.scholarship_name,
            payload.institution,
            payload.deadline,
        )
    except tracker_service.TodoGenerationError as exc:
        logger.exception(
            "To-do list generation failed for scholarship %r", payload.scholarship_name
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate preparation checklist: {exc}",
        ) from exc


@router.get("/", response_model=list[TrackerOut])
async def list_trackers(current_user: CurrentUser, db: DbSession) -> list[ApplicationTracker]:
    return await tracker_service.get_user_trackers(db, current_user.id)


@router.patch("/{tracker_id}/status", response_model=TrackerOut)
async def update_tracker_status(
    tracker_id: uuid.UUID,
    payload: TrackerStatusUpdate,
    current_user: CurrentUser,
    db: DbSession,
) -> ApplicationTracker:
    try:
        return await tracker_service.update_tracker_status(
            db, current_user.id, tracker_id, payload.new_status
        )
    except tracker_service.TrackerNotFoundError as exc:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found"
        ) from exc


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
