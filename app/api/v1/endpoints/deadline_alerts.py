import io
import logging
import re
import uuid

from fastapi import APIRouter, HTTPException, status
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.application_tracker import ApplicationTracker
from app.models.deadline_alert import DeadlineAlert
from app.models.user import User
from app.schemas.deadline_alert import DeadlineAlertOut
from app.services import deadline_alerts as alerts_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/alerts", tags=["deadline-alerts"])


async def _get_owned_tracker_or_404(
    db: DbSession, current_user: User, tracker_id: uuid.UUID
) -> ApplicationTracker:
    """Same "don't distinguish nonexistent from not-yours" 404 posture as
    every other user-scoped lookup in this codebase. Lives here (not
    imported from application_tracker.py) since that module's own
    equivalent helper is private (`_get_owned_tracker`) and DB-session-
    scoped identically -- duplicating a five-line query beats reaching
    into another router's private helper."""
    result = await db.execute(
        select(ApplicationTracker).where(
            ApplicationTracker.id == tracker_id, ApplicationTracker.user_id == current_user.id
        )
    )
    tracker = result.scalar_one_or_none()
    if tracker is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Tracker not found")
    return tracker


@router.post(
    "/generate/{tracker_id}", response_model=list[DeadlineAlertOut], status_code=status.HTTP_201_CREATED
)
async def generate_alerts(
    tracker_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> list[DeadlineAlert]:
    """Reads the deadline and scholarship name from the tracker itself,
    per the ask -- the caller only supplies `tracker_id` via the URL."""
    tracker = await _get_owned_tracker_or_404(db, current_user, tracker_id)

    if tracker.deadline is None:
        # Disclosed guard, not in the ask -- there's nothing to plan
        # backward from without a deadline. Failing fast with a clear
        # 422 is more actionable than either a 500 from downstream date
        # arithmetic on `None`, or silently generating nonsensical
        # "milestones" with no real anchor date.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This tracker has no deadline set yet -- set one before generating milestones.",
        )

    try:
        return await alerts_service.generate_milestones(
            db, current_user.id, tracker.id, tracker.scholarship_name, tracker.deadline
        )
    except alerts_service.MilestoneGenerationError as exc:
        logger.exception("Milestone generation failed for tracker %s", tracker_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail=f"Failed to generate milestone alerts: {exc}",
        ) from exc


@router.get("/upcoming", response_model=list[DeadlineAlertOut])
async def upcoming_alerts(current_user: CurrentUser, db: DbSession) -> list[DeadlineAlert]:
    return await alerts_service.get_upcoming_alerts(db, current_user.id)


@router.get("/export-ics/{tracker_id}")
async def export_ics(
    tracker_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> StreamingResponse:
    tracker = await _get_owned_tracker_or_404(db, current_user, tracker_id)
    alerts = await alerts_service.get_tracker_alerts(db, current_user.id, tracker.id)

    if not alerts:
        # Disclosed guard, not in the ask -- an empty .ics file is
        # technically valid RFC 5545 but a confusing, silent no-op from
        # the caller's perspective (they'd get a "successful" download
        # with nothing in it). A clear 404 is more actionable than that.
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="No milestone alerts exist for this tracker yet -- "
            "generate them first via POST /alerts/generate/{tracker_id}.",
        )

    ics_content = alerts_service.export_to_ics(alerts)

    # Sanitized filename -- a scholarship name can contain characters
    # (quotes, slashes, etc.) that aren't safe to drop unescaped into a
    # Content-Disposition header.
    safe_name = re.sub(r"[^A-Za-z0-9_.-]", "_", tracker.scholarship_name).strip("_") or "milestones"
    filename = f"{safe_name}_milestones.ics"

    return StreamingResponse(
        io.BytesIO(ics_content.encode("utf-8")),
        media_type="text/calendar",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.patch("/{alert_id}/complete", response_model=DeadlineAlertOut)
async def complete_alert(
    alert_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> DeadlineAlert:
    try:
        return await alerts_service.complete_alert(db, current_user.id, alert_id)
    except alerts_service.AlertNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found") from exc


@router.delete("/{alert_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_alert(alert_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> None:
    try:
        await alerts_service.delete_alert(db, current_user.id, alert_id)
    except alerts_service.AlertNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Alert not found") from exc
