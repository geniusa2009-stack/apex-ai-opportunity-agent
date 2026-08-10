"""Video Analytics, Watch Time Tracking & Engagement Engine -- service
layer. Owns the atomic upsert + completion-threshold logic behind
`POST /api/v1/analytics/heartbeat`, kept out of the router per this
codebase's uniform 3-layer convention (Models/Schemas -> Service -> Router).
"""

import logging
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.analytics import VideoPlaybackLog
from app.models.courses import Lesson
from app.models.enums import VideoType
from app.models.student_videos import StudentVideo
from app.schemas.analytics import HeartbeatRequest, HeartbeatResponse, VideoPlaybackProgressOut
from app.services import course_service, gamification_service

logger = logging.getLogger(__name__)

# This milestone's own literal business logic: "current_time_seconds /
# video_duration >= 0.95".
COMPLETION_THRESHOLD_RATIO = 0.95


class VideoNotFoundError(Exception):
    """Raised when a heartbeat's `(video_id, video_type)` doesn't resolve
    to a real row in the table its `video_type` implies. Kept as a
    dedicated exception (rather than silently creating a playback log
    row for a video that doesn't exist) so a typo'd/stale `video_id`
    fails loudly with a `404` instead of quietly accumulating orphaned
    analytics data for nothing -- see this module's own `record_heartbeat`
    docstring for the fuller reasoning.
    """


async def _resolve_video_duration_seconds(
    db: AsyncSession, video_id: UUID, video_type: VideoType
) -> float | None:
    """Looks up the video's own row in the table `video_type` implies and
    returns its duration -- `None` if the video exists but its pipeline
    hasn't resolved a duration yet (e.g. a `StudentVideo` still
    `PENDING_AI`, or a `Lesson` whose admin-upload transcode hasn't
    finished), which is a real, reachable, non-error state distinct from
    "this video doesn't exist at all" (`VideoNotFoundError`).

    Disclosed judgment call: duration is resolved HERE, server-side, from
    the video's own persisted row -- NOT taken from the heartbeat
    payload, which (per this milestone's own literal spec) never
    includes one. This is a deliberate strengthening, not just a literal
    reading: trusting a client-supplied duration for a computation that
    can flip `is_completed` (and, for a COURSE video, trigger a real
    token reward) would let a malicious client claim an arbitrarily short
    fake duration to farm completions instantly. Resolving it from the
    database instead means the completion threshold is always computed
    against the video's REAL, pipeline-determined duration.
    """
    if video_type == VideoType.UGC:
        video = await db.get(StudentVideo, video_id)
        if video is None:
            raise VideoNotFoundError(str(video_id))
        return float(video.duration_seconds) if video.duration_seconds is not None else None

    lesson = await db.get(Lesson, video_id)
    if lesson is None:
        raise VideoNotFoundError(str(video_id))
    return float(lesson.duration_seconds) if lesson.duration_seconds is not None else None


async def _lock_or_create_playback_log(
    db: AsyncSession, user_id: UUID, video_id: UUID, video_type_value: str
) -> VideoPlaybackLog:
    """Returns this (user, video)'s `VideoPlaybackLog` row, ROW-LOCKED
    (`SELECT ... FOR UPDATE`) within the CURRENTLY OPEN transaction, so
    every field mutation the caller makes afterward and the eventual
    single `db.commit()` both happen atomically against a lock that was
    never released in between -- see this module's own `record_heartbeat`
    docstring for why this differs from `coach_service.
    get_or_create_accountability`'s own similar-looking helper (which
    commits immediately on the create path, a real, disclosed difference,
    not an oversight).

    Uses `db.flush()`, NOT `db.commit()`, on the create path -- a flush
    assigns the new row's server-generated defaults (`id`, `created_at`)
    and runs the unique-constraint check against Postgres without ending
    the transaction (a `commit()` would release any lock this insert
    itself implicitly holds, reopening the exact race window this
    function exists to close). The first-ever-row race (two concurrent
    heartbeats for the same brand-new (user, video) pair both seeing no
    existing row and both attempting an insert) is handled the same
    "catch the loser's `IntegrityError`, roll back, re-select with
    `FOR UPDATE`" pattern as `coach_service.get_or_create_accountability`/
    `course_service.enroll_in_course` -- the loser's re-`SELECT ...
    FOR UPDATE` will simply block until the winner's transaction commits,
    then proceed against the winner's now-committed, now-lockable row.
    """
    result = await db.execute(
        select(VideoPlaybackLog)
        .where(
            VideoPlaybackLog.user_id == user_id,
            VideoPlaybackLog.video_id == video_id,
            VideoPlaybackLog.video_type == video_type_value,
        )
        .with_for_update()
    )
    log = result.scalar_one_or_none()
    if log is not None:
        return log

    log = VideoPlaybackLog(user_id=user_id, video_id=video_id, video_type=video_type_value)
    db.add(log)
    try:
        await db.flush()
    except IntegrityError:
        await db.rollback()
        result = await db.execute(
            select(VideoPlaybackLog)
            .where(
                VideoPlaybackLog.user_id == user_id,
                VideoPlaybackLog.video_id == video_id,
                VideoPlaybackLog.video_type == video_type_value,
            )
            .with_for_update()
        )
        log = result.scalar_one()

    return log


async def _award_lesson_completion_best_effort(db: AsyncSession, user_id: UUID, lesson_id: UUID) -> int:
    """Reuses the EXISTING lesson-completion reward pathway
    (`course_service.complete_lesson`) rather than inventing a second,
    parallel reward mechanism -- per this milestone's own "trigger the
    pre-existing completion reward callbacks if applicable" instruction.
    `complete_lesson` is already idempotent (a second call against an
    already-completed lesson returns `tokens_earned: 0` and awards
    nothing further), which is exactly the safety property needed here
    too: a video that's already `is_completed=True` before this call
    (see `record_heartbeat`'s own `newly_completed` guard) never reaches
    this function a second time, but even if it somehow did, no double
    award could occur.

    Best-effort, same disclosed posture as `student_video_service.
    resolve_thumbnail_url`: a student watching a COURSE video to 95%
    without ever actually enrolling (e.g. a public/preview video_id
    reachable some other way, or a lesson deleted mid-playback) is a
    real, reachable edge case that must NOT fail the surrounding
    heartbeat/analytics write -- watch-time tracking itself has already
    succeeded by the time this is called, and a missing reward is a far
    lower-stakes failure than corrupting or losing that write.

    Gamification: Daily Streaks, Badges, and Leaderboard addition --
    ALSO awards `gamification_service.COURSE_COMPLETION_XP` on a
    genuinely NEW completion (`not result["already_completed"]`), this
    step's own literal "course completions" XP trigger. Deliberately
    does NOT also call `gamification_service.process_daily_activity`
    here -- the video-watch heartbeat that led here already touches the
    streak unconditionally in `record_heartbeat` (see that function's
    own comment), so touching it a second time here would be redundant,
    not incorrect, but pointless extra work in the same request.
    Wrapped in its own `try/except` so a `GamificationError` (or any
    other unexpected failure in a brand-new subsystem) can never take
    down the pre-existing, already-shipped token-reward path this
    function's return value backs.
    """
    try:
        result = await course_service.complete_lesson(db, user_id, lesson_id)
    except (course_service.NotEnrolledError, course_service.LessonNotFoundError) as exc:
        logger.info(
            "Video analytics: skipping lesson-completion reward for lesson %s (user %s): %s",
            lesson_id,
            user_id,
            exc,
        )
        return 0

    if not result["already_completed"]:
        try:
            await gamification_service.add_xp(
                db, user_id, gamification_service.COURSE_COMPLETION_XP, "course_completion"
            )
        except Exception:
            logger.exception(
                "Gamification: failed to award course-completion XP for lesson %s (user %s)",
                lesson_id, user_id,
            )

    return int(result["tokens_earned"])


async def _touch_gamification_streak_best_effort(db: AsyncSession, user_id: UUID) -> None:
    """Gamification: Daily Streaks, Badges, and Leaderboard addition --
    this step's own literal "watches a video" trigger for
    `process_daily_activity`. Called unconditionally from every
    `record_heartbeat` call (not just a newly-completed one): "watching
    a video" is the act of playback itself, not a completion event --
    completions are a separate, stronger signal already handled by
    `_award_lesson_completion_best_effort`'s own XP award, above.

    `process_daily_activity` is cheap to call repeatedly in practice --
    its own same-day case is a single indexed row lookup with no write
    at all (see that function's own docstring) -- so calling it on every
    heartbeat (already throttled by the frontend to roughly once per 10
    watched seconds, per `useIpfsHlsPlayer.ts`'s `HEARTBEAT_FLUSH_
    THRESHOLD_SECONDS`) is not the same class of concern as the
    per-REQUEST cost this step's own `POST /gamification/daily-login`
    docstring disclaims calling it from `get_current_user`.

    Best-effort, same reasoning as `_award_lesson_completion_best_effort`
    above -- a brand-new subsystem's failure must never break the
    pre-existing watch-time tracking this function is called alongside.
    """
    try:
        await gamification_service.process_daily_activity(db, user_id)
    except Exception:
        logger.exception("Gamification: failed to record daily activity for user %s", user_id)


async def record_heartbeat(db: AsyncSession, user_id: UUID, payload: HeartbeatRequest) -> HeartbeatResponse:
    """The full atomic upsert + completion-threshold business logic
    behind `POST /api/v1/analytics/heartbeat`, per this milestone's own
    literal spec:

    1. Atomic upsert on `VideoPlaybackLog` for the (user, video) pair.
    2. `last_position_seconds = current_time_seconds` (clamped into
       `[0, duration_seconds]` when the duration is known -- a disclosed
       defensive addition: an un-clamped value could otherwise store a
       "resume position" past the end of the video, e.g. from a client
       reporting `currentTime` a moment after a real, valid seek to the
       very end but before the `ended` event fires).
    3. `cumulative_watch_time_seconds += delta_duration_seconds`
       (`delta_duration_seconds` itself is already bounded at the
       Pydantic layer -- see `schemas/analytics.py`'s
       `MAX_HEARTBEAT_DELTA_SECONDS`).
    4. `is_ended OR current_time_seconds / duration >= 0.95` flips
       `is_completed` to `True` -- but ONLY on the transition from
       `False` to `True` (a video that's already completed, then
       continues being watched/re-watched, must not repeatedly attempt
       (and repeatedly no-op against) the completion reward callback on
       every subsequent heartbeat).

    Raises `VideoNotFoundError` if `(video_id, video_type)` doesn't
    resolve to a real row -- the router maps this to a `404`.
    """
    duration_seconds = await _resolve_video_duration_seconds(db, payload.video_id, payload.video_type)

    log = await _lock_or_create_playback_log(db, user_id, payload.video_id, payload.video_type.value)

    clamped_position = max(0.0, payload.current_time_seconds)
    if duration_seconds is not None and duration_seconds > 0:
        clamped_position = min(clamped_position, duration_seconds)
    log.last_position_seconds = clamped_position

    log.cumulative_watch_time_seconds += payload.delta_duration_seconds

    newly_completed = False
    if not log.is_completed:
        crossed_threshold = (
            duration_seconds is not None
            and duration_seconds > 0
            and (payload.current_time_seconds / duration_seconds) >= COMPLETION_THRESHOLD_RATIO
        )
        if payload.is_ended or crossed_threshold:
            log.is_completed = True
            newly_completed = True

    db.add(log)
    await db.commit()
    await db.refresh(log)

    # Gamification: Daily Streaks, Badges, and Leaderboard addition --
    # every heartbeat (not just a newly-completed one) counts as "watched
    # a video" for streak purposes; see this call's own docstring.
    await _touch_gamification_streak_best_effort(db, user_id)

    tokens_earned = 0
    if newly_completed and payload.video_type == VideoType.COURSE:
        tokens_earned = await _award_lesson_completion_best_effort(db, user_id, payload.video_id)

    return HeartbeatResponse(
        video_id=log.video_id,
        video_type=VideoType(log.video_type),
        last_position_seconds=log.last_position_seconds,
        cumulative_watch_time_seconds=log.cumulative_watch_time_seconds,
        is_completed=log.is_completed,
        tokens_earned=tokens_earned,
    )


async def get_playback_progress(
    db: AsyncSession, user_id: UUID, video_id: UUID, video_type: VideoType
) -> VideoPlaybackProgressOut:
    """Backs `GET /api/v1/analytics/video/{video_type}/{video_id}` --
    the frontend's own explicit "Resume Playback Feature" requirement
    ("check if the backend has a historical last_position_seconds for
    this user + video"). Returns a real, all-zero `VideoPlaybackProgressOut`
    (never a `404`) when no row exists yet -- a viewer who has never
    watched any of this video before is not an error condition, it's the
    ordinary "start from the beginning" case, and `last_position_seconds:
    0.0` already means exactly that to the player -- forcing every caller
    to special-case a `404` into the same "start from 0" behavior would
    just be extra, pointless client-side branching for a case this
    response shape already represents naturally.
    """
    result = await db.execute(
        select(VideoPlaybackLog).where(
            VideoPlaybackLog.user_id == user_id,
            VideoPlaybackLog.video_id == video_id,
            VideoPlaybackLog.video_type == video_type.value,
        )
    )
    log = result.scalar_one_or_none()

    if log is None:
        return VideoPlaybackProgressOut(
            video_id=video_id,
            video_type=video_type,
            last_position_seconds=0.0,
            cumulative_watch_time_seconds=0.0,
            is_completed=False,
        )

    return VideoPlaybackProgressOut(
        video_id=log.video_id,
        video_type=VideoType(log.video_type),
        last_position_seconds=log.last_position_seconds,
        cumulative_watch_time_seconds=log.cumulative_watch_time_seconds,
        is_completed=log.is_completed,
    )
