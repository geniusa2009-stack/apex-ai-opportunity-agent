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

from app.api.deps import (
    CurrentUser,
    DbSession,
    RequireModerationAccess,
    RequireSeniorModerationAccess,
)
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.schemas.opportunity import PaginatedResponse
from app.schemas.student_videos import (
    AdminOverrideRequest,
    AdminOverrideResponse,
    AdminRejectAndPurgeRequest,
    AdminVideoReviewOut,
    AIFeedback,
)
from app.services import gamification_service, notification_service
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


def _build_review_item(video: StudentVideo, student_name: str | None) -> AdminVideoReviewOut:
    """Turns one `StudentVideo` ORM row (plus its uploader's resolved
    `full_name`) into one `AdminVideoReviewOut`, correctly splitting the
    single `ai_feedback` JSONB column into either real Gemini feedback
    (`REJECTED`) or a pipeline-error message (`FAILED`) -- see
    `AdminVideoReviewOut`'s own docstring for why this can't just be a
    `model_validate(video)` call.
    """
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

    result = await db.execute(
        select(StudentVideo, User.full_name)
        .outerjoin(User, User.id == StudentVideo.student_id)
        .where(*filters)
        .order_by(StudentVideo.created_at.asc())
        .limit(limit)
        .offset(offset)
    )

    items = [_build_review_item(video, student_name) for video, student_name in result.all()]

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

    return AdminOverrideResponse(
        success=True, video_id=video.id, new_status=StudentVideoStatus(video.status)
    )


@router.delete("/admin/videos/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_video_permanently(
    video_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> None:
    """Senior-Moderator-or-higher (Real Tiered Role System step -- this
    action triggers real, irreversible R2/storage deletion on top of the
    row delete itself, which `AdminRole.MODERATOR`'s own docstring
    explains is deliberately out of scope for the entry tier). Permanently
    deletes ANY student's `StudentVideo` row --
    see this module's own docstring for why this is a NEW, separate
    endpoint from `override_video_decision`, not a side effect of
    rejecting a video. No ownership check (same posture as
    `override_video_decision`): by definition, ANY admin may delete ANY
    student's video, already fully handled by `RequireAdmin`.

    Cloud Garbage Collection (IPFS Unpinning & R2 Deletion): captures
    `video.hls_master_url`/`video.thumbnail_url` BEFORE deleting the row,
    then dispatches `task_delete_orphaned_media` (`app/workers/
    cleanup_tasks.py`) fire-and-forget ONLY AFTER a successful commit --
    same disclosed ordering reasoning as `student_videos.py`'s own
    `delete_student_video` (its docstring has the fuller version): a
    confirmed DB delete is the correct trigger for "the storage may now
    be cleaned up," not the other way around, and the Celery dispatch
    remains non-blocking regardless of which side of the commit it sits
    on. `StudentVideo` has no IPFS CID columns (see the Student Video
    Pinata -> Cloudflare R2 Migration step) -- only R2 urls are ever
    passed here; `ipfs_cids` is intentionally never populated by this
    endpoint.

    404s for a video that doesn't exist -- same posture as every other
    `{video_id}`-parameterized route in this router and in
    `student_videos.py`.
    """
    video = await db.get(StudentVideo, video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student video not found")

    r2_urls = [url for url in (video.hls_master_url, video.thumbnail_url) if url]

    await db.delete(video)
    await db.commit()

    if r2_urls:
        task_delete_orphaned_media.delay(r2_urls=r2_urls)


@router.post(
    "/admin/videos/{video_id}/reject-and-purge-media",
    response_model=AdminOverrideResponse,
)
async def reject_video_and_purge_media(
    video_id: uuid.UUID,
    payload: AdminRejectAndPurgeRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireSeniorModerationAccess,
) -> AdminOverrideResponse:
    """Senior-Moderator-or-higher (Real Tiered Role System step -- same
    "triggers irreversible storage deletion" reasoning as
    `delete_video_permanently` above). Student Dashboard & Admin Control Panel step's own
    literal "Reject & Delete" action: "Updates the DB to REJECTED, and
    critically, triggers the existing `task_delete_orphaned_media` to
    unpin from IPFS / delete from R2, ensuring zero storage bloat."

    DELIBERATELY DISTINCT from both existing mutations on this router:
    - `override_video_decision` (`target_status=REJECTED`) sets the same
      status but NEVER touches storage -- by design, since a REJECTED
      video's own media must stay retrievable for the moderation
      queue's preview modal (see this module's own docstring on why
      `DELETE /admin/videos/{video_id}` had to be added as a SEPARATE
      endpoint rather than folded into that one, for the identical
      reason).
    - `delete_video_permanently` purges storage but also deletes the
      ROW itself -- this action keeps the row (REJECTED, with the
      admin's own notes attached) as a permanent moderation record,
      purging only the underlying media, per this step's own literal
      "Updates the DB to REJECTED" wording (not "deletes the record").

    DISCLOSED NECESSARY ADDITION beyond the ask's own literal wording:
    after dispatching cleanup, `hls_master_url`/`thumbnail_url` are set
    to `None` on the row. Leaving the old R2 URLs in place after their
    underlying objects have just been deleted would mean this row
    silently starts pointing at storage that no longer exists -- a
    dangling reference the ask's own "ensuring zero storage bloat"
    goal implies should be cleaned up structurally, not just in the
    object store. `admin_override`/`admin_notes` are set exactly like
    `override_video_decision`'s own REJECTED path.

    No "already REJECTED" guard the way `override_video_decision` guards
    its APPROVED-XP-award branch -- this action is idempotent-ish by
    nature (calling it twice on an already-purged video simply finds
    `r2_urls` empty the second time and dispatches nothing), and no
    notification is sent for a rejection (this step's own ask only names
    a notification for the "Approve" action, not this one -- consistent
    with the existing pipeline's own behavior, which also never notifies
    a student of a REJECTED outcome; see `notification_service`'s own
    trigger points).
    """
    video = await db.get(StudentVideo, video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student video not found")

    r2_urls = [url for url in (video.hls_master_url, video.thumbnail_url) if url]

    video.status = StudentVideoStatus.REJECTED.value
    video.admin_override = True
    video.admin_notes = payload.notes
    video.hls_master_url = None
    video.thumbnail_url = None

    db.add(video)
    await db.commit()
    await db.refresh(video)

    if r2_urls:
        task_delete_orphaned_media.delay(r2_urls=r2_urls)

    return AdminOverrideResponse(
        success=True, video_id=video.id, new_status=StudentVideoStatus(video.status)
    )
