"""Admin Override Dashboard & AI Decision Correction System.

Four endpoints, letting a human moderator audit, manually correct, and
permanently remove flagged `StudentVideo` decisions. As of the Real
Tiered Role System step, these are no longer uniformly `RequireAdmin`
(`is_superuser`-only)-gated -- `get_review_queue`/`override_video_decision`
are `RequireModerationAccess` (Moderator tier or higher; reviewable,
reversible actions), while `delete_video_permanently`/
`reject_video_and_purge_media` are `RequireSeniorModerationAccess`
(Senior Moderator tier or higher; both trigger real, irreversible
storage deletion) -- see `AdminRole`'s own docstring
(app/models/enums.py) for the full tier reasoning:

    GET    /admin/videos/review-queue                    -- paginated queue of flagged videos
    POST   /admin/videos/{video_id}/override             -- manually set a video's final status
    DELETE /admin/videos/{video_id}                      -- permanently delete a video (Cloud GC)
    POST   /admin/videos/{video_id}/reject-and-purge-media -- reject + purge storage, keep the row
                                                             (Student Dashboard & Admin Control Panel
                                                             step's own "Reject & Delete" action --
                                                             see that route's own docstring)

No shared `prefix` on the `APIRouter` -- same posture as `student_videos.py`/
`media_router.py`: each route's full literal path is written directly in
its own decorator.

Disclosed routing-precedence check (same discipline as every other
router in this codebase): this router's paths all start with the literal
segment `admin`, but none collide with `media_router.py`'s own admin-only
route (`POST /admin/courses/{course_id}/lessons/{lesson_id}/
upload-video`) -- `admin/videos/review-queue`, `admin/videos/{video_id}/override`,
`admin/videos/{video_id}` (DELETE), and `admin/courses/{course_id}/...`
diverge at the SECOND path segment (`videos` vs. `courses`), so there is
no actual FastAPI/Starlette path-template ambiguity between any of these
routes. Confirmed empirically anyway (see the README's verification
section), not just reasoned about.

Cloud Garbage Collection (IPFS Unpinning & R2 Deletion) step, DISCLOSED
NEW ENDPOINT: `DELETE /admin/videos/{video_id}` did NOT exist before this
step -- this router previously had exactly the two GET/POST routes above.
This step's own ask names `admin_moderation.py` as a place to wire in
storage cleanup ("when a video record is permanently deleted -- e.g. an
Admin rejects/deletes a Student UGC video"), but the ONLY existing
mutation this router had was `override_video_decision`, which flips
`status`/`admin_override`/`admin_notes` and NEVER deletes the underlying
row -- an admin "rejecting" a video is a moderation decision, not a
deletion, and the review-queue's own "preview flagged content" feature
(the frontend's `ModerationQueueClient.tsx` preview modal) depends on a
REJECTED video's media still existing so an admin can actually watch it
before deciding whether to override. Wiring storage deletion into
`override_video_decision`'s reject path would have been a real,
undisclosed regression to that existing feature. Instead, this step adds
the genuinely missing "permanently delete" action itself -- a new,
separate `DELETE /admin/videos/{video_id}` route, distinct from
`override_video_decision`, so an admin has an explicit, deliberate way to
remove a video (e.g. spam, a repeat-offender upload, or a REJECTED video
they no longer want occupying the review queue or any storage) as
opposed to merely marking it reviewed.

Gamification: Daily Streaks, Badges, and Leaderboard addition --
`override_video_decision` is one of TWO places a `StudentVideo` can
transition into `APPROVED` in this codebase (the other being the AI
pipeline's own success branch, `app/tasks/student_video.py`); both now
award `gamification_service.UGC_APPROVAL_XP` on a genuinely NEW
transition into `APPROVED`. See `override_video_decision`'s own
updated docstring for why this endpoint specifically needs a
"was it already APPROVED before this call" guard that the AI pipeline's
own award does not.
"""

import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.orm import aliased

from app.api.deps import (
    CurrentUser,
    DbSession,
    RequireModerationAccess,
    RequireSeniorModerationAccess,
    effective_admin_role,
)
from app.models.enums import ModerationAction, ModerationTargetType, StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.schemas.dangerous_actions import DangerousActionPendingResponse
from app.schemas.opportunity import PaginatedResponse
from app.schemas.student_videos import (
    AdminDeleteVideoRequest,
    AdminOverrideRequest,
    AdminOverrideResponse,
    AdminRejectAndPurgeRequest,
    AdminVideoReviewOut,
    AIFeedback,
)
from app.services import (
    dangerous_action_service,
    gamification_service,
    moderation_claim_service,
    moderation_log_service,
    notification_service,
    repeat_offender_service,
)
from app.workers.cleanup_tasks import task_delete_orphaned_media

logger = logging.getLogger(__name__)

router = APIRouter(tags=["admin-moderation"])

# The three statuses this step's own ask names explicitly for the review
# queue -- everything an admin might reasonably need to look at:
# `PENDING_AI` (still awaiting/stuck in the pipeline), `REJECTED`
# (Gemini flagged it), `FAILED` (the pipeline itself broke). `APPROVED`
# is deliberately excluded -- an already-approved video needs no admin
# attention, matching this step's own literal filter.
_REVIEW_QUEUE_STATUSES = [
    StudentVideoStatus.REJECTED.value,
    StudentVideoStatus.FAILED.value,
    StudentVideoStatus.PENDING_AI.value,
]

# Fields `AIFeedback` actually defines -- used below to filter a raw
# `ai_feedback` JSONB dict down to only the keys that schema accepts
# before attempting to construct one, since the real column also
# sometimes carries a `transcode_metadata` key (written alongside the
# real Gemini fields on a successful pipeline run) that `AIFeedback`
# itself has no field for. See `AdminVideoReviewOut`'s own docstring for
# the fuller reasoning on why this split exists at all.
_AI_FEEDBACK_FIELDS = set(AIFeedback.model_fields)


def _build_review_item(
    video: StudentVideo,
    student_name: str | None,
    claimant_name: str | None = None,
    offender_recent_action_count: int = 0,
) -> AdminVideoReviewOut:
    """Turns one `StudentVideo` ORM row (plus its uploader's resolved
    `full_name`) into one `AdminVideoReviewOut`, correctly splitting the
    single `ai_feedback` JSONB column into either real Gemini feedback
    (`REJECTED`) or a pipeline-error message (`FAILED`) -- see
    `AdminVideoReviewOut`'s own docstring for why this can't just be a
    `model_validate(video)` call.

    Claim System addition: `claimant_name` is the SECOND outer-joined
    `User.full_name` (the claimant's, not the uploader's -- see
    `get_review_queue`'s own query for the `aliased(User)` this needs,
    since both joins target the same `users` table). Claim state itself
    is resolved through `moderation_claim_service.get_effective_claim`
    below, NOT read straight off `video.claimed_by`/`video.claimed_at`
    -- an expired claim must render as unclaimed here, same as it does
    everywhere else this feature reads claim state.
    """
    effective_claimed_by, effective_claimed_at = moderation_claim_service.get_effective_claim(video)
    ai_feedback: AIFeedback | None = None
    pipeline_error: str | None = None

    if video.ai_feedback:
        if video.status == StudentVideoStatus.FAILED.value:
            pipeline_error = video.ai_feedback.get("error")
        else:
            filtered = {k: v for k, v in video.ai_feedback.items() if k in _AI_FEEDBACK_FIELDS}
            try:
                ai_feedback = AIFeedback(**filtered)
            except ValidationError:
                # Defensive fallback -- a malformed/incomplete legacy
                # row that isn't cleanly one shape or the other. Surface
                # SOMETHING to the admin rather than silently dropping
                # it entirely.
                logger.warning("Video %s has malformed ai_feedback; surfacing raw", video.id)
                pipeline_error = str(video.ai_feedback)

    return AdminVideoReviewOut(
        id=video.id,
        title=video.title,
        description=video.description,
        student_id=video.student_id,
        student_name=student_name,
        status=StudentVideoStatus(video.status),
        created_at=video.created_at,
        hls_master_url=video.hls_master_url,
        thumbnail_url=video.thumbnail_url,
        ai_score=video.ai_score,
        ai_feedback=ai_feedback,
        pipeline_error=pipeline_error,
        admin_override=video.admin_override,
        admin_notes=video.admin_notes,
        claimed_by=effective_claimed_by,
        claimed_by_name=claimant_name if effective_claimed_by is not None else None,
        claimed_at=effective_claimed_at,
        offender_recent_action_count=offender_recent_action_count,
    )


@router.get("/admin/videos/review-queue", response_model=PaginatedResponse[AdminVideoReviewOut])
async def get_review_queue(
    db: DbSession,
    current_user: CurrentUser,
    _: RequireModerationAccess,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[AdminVideoReviewOut]:
    """Admin-only. Every `StudentVideo` currently in `REJECTED`,
    `FAILED`, or `PENDING_AI`, oldest first (`ORDER BY created_at ASC`,
    per this step's own literal wording -- the longest-waiting item
    surfaces first, so nothing sits in the queue indefinitely just
    because newer flags keep landing on top of it).

    `current_user: CurrentUser` is declared but not read directly in
    this handler's body -- same established convention as every other
    `RequireAdmin`-gated route in this codebase (e.g. `courses.py`'s
    `create_course`), kept for consistency and as a natural extension
    point if a future step needs to log/attribute WHICH admin viewed the
    queue.

    `LEFT OUTER JOIN` (not an inner join) against `users` for
    `student_name` -- an inner join would silently DROP a video whose
    uploader account was since deleted (a real, reachable case: `User`
    deletion cascades onto `StudentVideo` via `ondelete="CASCADE"`... but
    only when the DELETE goes through the ORM/DB relationship; a video
    could still legitimately end up here with no matching user row under
    other deletion paths). An outer join instead just yields `student_name
    = None` for that row, which `AdminVideoReviewOut.student_name`'s own
    `str | None` type already accounts for -- the video itself stays
    visible and reviewable either way.
    """
    filters = (StudentVideo.status.in_(_REVIEW_QUEUE_STATUSES),)

    total = (
        await db.execute(select(func.count()).select_from(StudentVideo).where(*filters))
    ).scalar_one()

    # Claim System addition: `Claimant` is a SECOND alias of `User` --
    # the uploader join (`User`) and the claimant join both target the
    # same `users` table, and SQL has no way to tell two joins against
    # the same table apart without aliasing one of them.
    Claimant = aliased(User)
    result = await db.execute(
        select(StudentVideo, User.full_name, Claimant.full_name)
        .outerjoin(User, User.id == StudentVideo.student_id)
        .outerjoin(Claimant, Claimant.id == StudentVideo.claimed_by)
        .where(*filters)
        .order_by(StudentVideo.created_at.asc())
        .limit(limit)
        .offset(offset)
    )

    rows = result.all()

    # Repeat-Offender Detection addition (migration 0041): ONE real query
    # for the whole page of results, not one per row -- see
    # `repeat_offender_service.get_recent_offense_counts`'s own docstring
    # for why this is a single bounded query rather than N.
    offense_counts = await repeat_offender_service.get_recent_offense_counts(db)

    items = [
        _build_review_item(
            video, student_name, claimant_name, offense_counts.get(video.student_id, 0)
        )
        for video, student_name, claimant_name in rows
    ]

    return PaginatedResponse[AdminVideoReviewOut](items=items, total=total, limit=limit, offset=offset)


@router.post("/admin/videos/{video_id}/override", response_model=AdminOverrideResponse)
async def override_video_decision(
    video_id: uuid.UUID,
    payload: AdminOverrideRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireModerationAccess,
) -> AdminOverrideResponse:
    """Moderator-or-higher. Manually sets `video.status` to `payload.target_status`
    (already validated to be `APPROVED` or `REJECTED` at the Pydantic
    layer -- see `AdminOverrideRequest`), flips `admin_override` to
    `True`, and stores `payload.notes` as `admin_notes` -- exactly this
    step's own literal "Fetch the video, update its status... set
    admin_override = True, and store the admin_notes" instruction.

    `admin_notes` is OVERWRITTEN (not appended to) on every call --
    consistent with it being one scalar column, not a list/audit table
    of every override this video has ever received (see
    `StudentVideo.admin_notes`'s own docstring for the disclosed scope
    boundary this implies). `payload.notes` being `None` (the caller
    omitted it) correctly clears any prior note rather than leaving a
    stale one behind from an earlier override.

    404s (not silently no-ops) for a video that doesn't exist -- same
    "can't reference a real thing" posture as every other `{video_id}`-
    parameterized route in `student_videos.py`. No ownership check here
    at all (unlike that file's owner-only routes) -- by definition, ANY
    admin may override ANY student's video; there is no "wrong owner"
    case for this endpoint, only "admin or not," already fully handled
    by `RequireAdmin`.
    """
    video = await db.get(StudentVideo, video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student video not found")

    # Gamification: Daily Streaks, Badges, and Leaderboard addition --
    # captured BEFORE mutating `video.status`, below. An admin can call
    # this endpoint on the SAME video more than once (e.g. correcting an
    # earlier override, or re-affirming a decision) -- unlike the AI
    # pipeline's own success branch (`app/tasks/student_video.py`, which
    # only ever transitions a video into APPROVED/REJECTED exactly once,
    # from PENDING_AI), this endpoint has no such guarantee, so XP must
    # only be awarded on a genuine NEW transition into APPROVED, not on
    # every override call that happens to already be (or re-affirm)
    # APPROVED.
    was_already_approved = video.status == StudentVideoStatus.APPROVED.value

    video.status = payload.target_status.value
    video.admin_override = True
    video.admin_notes = payload.notes

    db.add(video)

    # Automatic Moderation Activity Log step -- logged BEFORE the commit
    # below, same transaction as the video mutation itself (see
    # `moderation_log_service`'s own docstring for why this must be
    # atomic with the action, not a best-effort side effect). NEXT ADMIN
    # ACTION: add your own `moderation_log_service.log_action(...)` call
    # here at your own real mutation site, using a new
    # `ModerationAction` member (see that enum's own docstring).
    # Return value captured (Transparency: Notify Users addition) -- the
    # REJECT-branch notification below needs this exact row's real,
    # persisted `id` to embed as `log_entry_id` in its payload, so a
    # student's "appeal this" UI action can reference the exact real
    # action being appealed.
    log_entry = moderation_log_service.log_action(
        db,
        actor_id=current_user.id,
        actor_role=effective_admin_role(current_user),
        action=ModerationAction.VIDEO_OVERRIDE,
        target_type=ModerationTargetType.STUDENT_VIDEO,
        target_id=video.id,
        outcome={"new_status": video.status, "notes": video.admin_notes},
    )

    await db.commit()
    await db.refresh(video)

    # Best-effort, AFTER the commit above -- a new subsystem's failure
    # must never turn an otherwise-successful moderation override into a
    # failed request. See `app/tasks/student_video.py`'s own identical
    # UGC-approval XP award for the other, non-admin path into this
    # exact same reward.
    if payload.target_status == StudentVideoStatus.APPROVED and not was_already_approved:
        try:
            await gamification_service.add_xp(
                db, video.student_id, gamification_service.UGC_APPROVAL_XP, "ugc_approval"
            )
        except Exception:
            logger.exception(
                "Gamification: failed to award UGC-approval XP for video %s (student %s) via admin override",
                video_id, video.student_id,
            )

        # Smart Notifications Engine addition -- this step's own literal
        # "Video Approvals" trigger, at the SAME gate (genuine new
        # transition into APPROVED only) as the Gamification XP award
        # immediately above; see `app/tasks/student_video.py`'s own
        # identical trigger for the other, non-admin path into this
        # exact same notification. Best-effort, same reasoning.
        try:
            await notification_service.create_notification(
                db,
                video.student_id,
                "ACHIEVEMENT",
                "Your video was approved!",
                f'Your video "{video.title}" was approved by an admin and is now live on your profile.',
            )
        except Exception:
            logger.exception(
                "Notifications: failed to notify student %s of video %s approval via admin override",
                video.student_id, video_id,
            )

    # --- Transparency: Notify Users of Moderation Actions addition ---
    # The REJECT branch of this same endpoint -- confirmed, real gap:
    # before this task, a rejected override notified nobody at all (only
    # the APPROVE branch above ever did). Real, honest wording reuses
    # `payload.notes` when the moderator provided one; an explicit,
    # non-evasive fallback sentence when they didn't, rather than a vague
    # "something happened" message. `payload` embeds the real
    # `log_entry_id` + `appealable: True` so the frontend can deep-link
    # straight to a real appeal submission form -- see
    # `moderation_appeal_service.APPEALABLE_ACTIONS` for why
    # `VIDEO_OVERRIDE` (when rejecting) is genuinely appealable and
    # REVERSIBLE if upheld. Best-effort, same reasoning as the APPROVE
    # branch's own notification above.
    elif payload.target_status == StudentVideoStatus.REJECTED:
        try:
            reason_text = payload.notes or "No additional reason was provided by the reviewing moderator."
            await notification_service.create_notification(
                db,
                video.student_id,
                "WARNING",
                "Your video was rejected",
                f'Your video "{video.title}" was reviewed and rejected. Reason: {reason_text}',
                payload={
                    "type": "moderation_action",
                    "log_entry_id": str(log_entry.id),
                    "action": ModerationAction.VIDEO_OVERRIDE.value,
                    "appealable": True,
                },
            )
        except Exception:
            logger.exception(
                "Notifications: failed to notify student %s of video %s rejection via admin override",
                video.student_id, video_id,
            )

    return AdminOverrideResponse(
        success=True, video_id=video.id, new_status=StudentVideoStatus(video.status)
    )


@router.delete("/admin/videos/{video_id}", response_model=DangerousActionPendingResponse, status_code=status.HTTP_202_ACCEPTED)
async def delete_video_permanently(
    video_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
    payload: AdminDeleteVideoRequest | None = None,
) -> DangerousActionPendingResponse:
    """Senior-Moderator-or-higher. Real Two-Person Confirmation for
    Dangerous Actions addition (migration 0054) -- THIS ROUTE NO LONGER
    EXECUTES THE DELETE DIRECTLY. See `app/models/
    dangerous_action_confirmation.py`'s own module docstring, STEP 3, for
    the full "why" -- this permanently-destructive action now requires a
    real second Senior-Moderator-or-Owner approval (`POST /admin/
    moderation/dangerous-actions/{confirmation_id}/approve`) or a
    deliberate Owner override (`POST /admin/moderation/{item_type}/
    {item_id}/dangerous-actions/{action}/override`) before it actually
    runs. Calling this route creates (or, if one is already pending,
    idempotently returns) a real `DangerousActionConfirmation` row -- the
    video is NOT deleted by this call alone. The real deletion logic
    itself (R2 cleanup, `ModerationLogEntry`, notification) is
    UNCHANGED, just moved to `dangerous_action_service.
    _execute_video_delete`, run only once a second approval or override
    actually happens.
    """
    video = await db.get(StudentVideo, video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student video not found")

    notes = payload.notes if payload is not None else None
    confirmation, _created = await dangerous_action_service.request_confirmation(
        db,
        action=ModerationAction.VIDEO_DELETE,
        item_type=ModerationTargetType.STUDENT_VIDEO,
        item_id=video_id,
        notes=notes,
        requester=current_user,
        requester_role=effective_admin_role(current_user),
    )
    await db.commit()
    await db.refresh(confirmation)
    return DangerousActionPendingResponse(
        confirmation_id=confirmation.id,
        action=confirmation.action,
        item_type=confirmation.item_type,
        item_id=confirmation.item_id,
        status=confirmation.status,
        requested_by=confirmation.requested_by,
        requested_at=confirmation.requested_at,
    )


@router.post(
    "/admin/videos/{video_id}/reject-and-purge-media",
    response_model=DangerousActionPendingResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def reject_video_and_purge_media(
    video_id: uuid.UUID,
    payload: AdminRejectAndPurgeRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> DangerousActionPendingResponse:
    """Senior-Moderator-or-higher. Real Two-Person Confirmation for
    Dangerous Actions addition (migration 0054) -- same "no longer
    executes directly" change as `delete_video_permanently` immediately
    above; see that route's own docstring and `app/models/
    dangerous_action_confirmation.py`'s module docstring for the full
    reasoning. The real reject-and-purge logic itself is UNCHANGED, just
    moved to `dangerous_action_service._execute_video_reject_and_purge`.
    """
    video = await db.get(StudentVideo, video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student video not found")

    confirmation, _created = await dangerous_action_service.request_confirmation(
        db,
        action=ModerationAction.VIDEO_REJECT_AND_PURGE,
        item_type=ModerationTargetType.STUDENT_VIDEO,
        item_id=video_id,
        notes=payload.notes,
        requester=current_user,
        requester_role=effective_admin_role(current_user),
    )
    await db.commit()
    await db.refresh(confirmation)
    return DangerousActionPendingResponse(
        confirmation_id=confirmation.id,
        action=confirmation.action,
        item_type=confirmation.item_type,
        item_id=confirmation.item_id,
        status=confirmation.status,
        requested_by=confirmation.requested_by,
        requested_at=confirmation.requested_at,
    )
