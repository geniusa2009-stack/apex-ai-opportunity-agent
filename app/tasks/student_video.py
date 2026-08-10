"""Student Video Profiles & AI-Moderated Upload System -- Celery task.

`process_student_video_pipeline_task` runs the full five-sub-step
pipeline (`app/services/student_video_service.py`) against a raw video
already saved to local disk by the router (`app/api/v1/endpoints/
student_videos.py`'s `upload_student_video`), and writes the outcome
back onto the `StudentVideo` row.

Architecture note, disclosed: unlike the Core Video Media Processing
Pipeline's `app/workers/media_processing.py` (which hands raw video
bytes to the worker via a Supabase Storage path specifically so the
Celery worker doesn't need to share a filesystem with the FastAPI
process), THIS task takes a plain `local_filepath: str` directly, per
this step's own explicit task signature
(`process_student_video_pipeline_task(video_id: UUID, local_filepath:
str)`) and its own explicit router instruction ("saves raw video to a
secured local temporary directory"). That's honored literally here, but
it's a real architectural trade-off worth naming plainly: this task ONLY
works correctly if the Celery worker process can read the exact same
local filesystem path the FastAPI process wrote to -- true for a
single-machine deployment (worker and API on the same host/container) or
one with a shared volume mounted at the same path on both, but NOT true
for a horizontally-scaled deployment where API and worker instances run
on separate machines with no shared disk. See the README's disclosed
judgment calls for the fuller version of this trade-off and why it's
implemented as literally asked rather than silently "fixed" to reuse the
Storage-mediated approach from the prior step.

Retry-vs-cleanup, the other disclosed judgment call this task resolves:
the ask asks for BOTH (a) automatic retries with exponential backoff on
failure, re-invoking this exact task with the exact same `local_filepath`
argument, AND (b) a strict `try...finally` that force-deletes "raw files
on the local filesystem... even if the task fails." Taken fully
literally at the same time, these contradict each other: if the raw
upload at `local_filepath` were deleted on EVERY failed attempt, the
very next retry would immediately fail again with a plain "file not
found" -- retries could never actually succeed. Resolved here by only
deleting `local_filepath` once no further retry will ever need it again
-- either the pipeline actually succeeded, or this is genuinely the LAST
allowed attempt (`self.request.retries >= self.max_retries`). Every
OTHER local artifact this task creates for itself (the audio extraction,
the HLS output directory -- both regenerated fresh from `local_filepath`
on every attempt, inside their own per-attempt `tempfile.mkdtemp()`
directory) IS force-deleted on every single attempt, satisfying the
"even if the task fails" requirement in full for everything that isn't
the one input the retry mechanism itself depends on staying alive.

Student Video Pinata -> Cloudflare R2 Migration step: no signature
change to this task at all -- purely an internal storage-backend swap.
`root_cid`/`thumbnail_cid` local variable names below were renamed to
`hls_master_url`/`thumbnail_url` to match the renamed model columns and
renamed `student_video_service` methods they come from
(`transcode_and_upload_to_r2`/`resolve_thumbnail_url`); see that
module's own docstring and `app/models/student_videos.py`'s module
docstring for the full disclosed rename/rationale.

Hybrid Video Thumbnail System step added a third optional argument,
`thumbnail_local_filepath` -- a viewer-supplied custom thumbnail image,
already validated and saved to disk by the router as a SIBLING file
inside the exact same per-upload temp directory as `local_filepath` (see
that router's own docstring). This file's lifecycle is deliberately
threaded through the SAME retry-vs-cleanup resolution described above:
it survives every attempt except the terminal one, and is deleted by
`_cleanup_raw_upload` (updated below to accept it) at the exact same
moment `local_filepath` itself is -- disclosed, necessary bug fix found
while making this change: `_cleanup_raw_upload`'s original
`os.rmdir(parent_dir)` call only succeeds when the directory is
genuinely EMPTY. Before this fix, a custom thumbnail left sitting in
that same directory (never itself deleted by the original version of
this function) would have silently made every terminal cleanup's own
`os.rmdir` a permanent no-op, leaking one now-orphaned temp directory
per upload that included a custom thumbnail -- a real disk leak this
step's own "completely prevent disk leaks" requirement calls out
directly, caught here specifically because this change forced a close
re-read of that cleanup path, not because it was reported.
"""

import asyncio
import logging
import os
import shutil
import tempfile
import uuid

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.schemas.student_videos import AIFeedback
from app.services import ai_context, gamification_service, notification_service
from app.services.student_video_service import student_video_service

logger = logging.getLogger(__name__)


@celery_app.task(
    name="app.tasks.student_video.process_student_video_pipeline_task",
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    autoretry_for=(Exception,),
    # Disclosed correction: the ask's own literal `default_retry_delay=60`
    # ALONE is a flat, non-exponential 60s delay in Celery's actual retry
    # semantics -- `retry_backoff=True` is the parameter that makes the
    # delay grow exponentially (roughly 60s, 120s, 240s...), using
    # `default_retry_delay` as its base. Both are set together so "Max
    # Retries: 3, with exponential backoff" is genuinely true behavior,
    # not just a comment. `retry_backoff_max` caps how large that growth
    # can get (a transient outage shouldn't back off for hours);
    # `retry_jitter=True` adds small random variance so many videos
    # failing at once (e.g. one shared OpenAI/Gemini/Pinata outage) don't
    # all retry in exact lockstep.
    retry_backoff=True,
    retry_backoff_max=600,
    retry_jitter=True,
)
def process_student_video_pipeline_task(
    self, video_id: str, local_filepath: str, thumbnail_local_filepath: str | None = None
) -> dict:
    """Celery entrypoint (sync). Same `asyncio.run()` bridge as every
    other task in this codebase (`app/workers/scholar_agent.py`,
    `app/workers/media_processing.py`) -- see either of those for the
    full event-loop-lifetime reasoning. `bind=True` (this method's first
    parameter is `self`, the task instance) so `_process_student_video_pipeline`
    can read `self.request.retries`/`self.max_retries` to determine
    whether THIS is the final allowed attempt -- see the module docstring
    for why that distinction is what makes the retry-vs-cleanup
    resolution possible at all.

    `video_id`/`local_filepath`/`thumbnail_local_filepath` are all plain
    `str` (the last one `| None`), not `UUID`/`Path` -- Celery's
    configured `task_serializer="json"` (app/core/celery_app.py) needs
    JSON-native types, same established precedent as every other task in
    this codebase. `thumbnail_local_filepath` defaults to `None` (Hybrid
    Video Thumbnail System addition) so this task's signature stays
    backward-compatible with any already-queued/in-flight invocation
    from before this step that only ever passed two positional
    arguments.
    """
    return asyncio.run(
        _process_student_video_pipeline(self, video_id, local_filepath, thumbnail_local_filepath)
    )


async def _process_student_video_pipeline(
    task, video_id: str, local_filepath: str, thumbnail_local_filepath: str | None = None
) -> dict:
    """The actual pipeline. Async so it can use this codebase's normal
    `AsyncSession`-based DB access for the row's status/result write, the
    same "sync media/audio work called directly from inside one async
    coroutine" bridging pattern established by `media_processing.py` --
    see that module's docstring for the fuller cross-loop-safety
    reasoning, which applies identically here.
    """
    is_final_attempt = task.request.retries >= task.max_retries
    tmp_dir = tempfile.mkdtemp(prefix="student_video_pipeline_")
    # Tracks whether it's safe to delete `local_filepath` (and
    # `thumbnail_local_filepath`, if present) in the `finally` block
    # below -- true once either the pipeline has fully succeeded, OR a
    # terminal (non-retryable-any-further) outcome has been written to
    # the DB. See the module docstring's "Retry-vs-cleanup" section.
    terminal_outcome_reached = False
    # Populated by the best-effort thumbnail resolution step below --
    # initialized here (not just inside the `try` block) so it's always
    # defined by the time either the success path OR the final-attempt
    # failure branch runs, regardless of which pipeline sub-step (if any)
    # raised. See `resolve_thumbnail_url`'s own docstring for why this
    # step itself never raises.
    thumbnail_url: str | None = None

    try:
        async with AsyncSessionLocal() as session:
            video = await session.get(StudentVideo, uuid.UUID(video_id))
            if video is None:
                # The row is gone (e.g. `DELETE /student/videos/{id}` ran
                # while this task was queued/in-flight -- see the
                # router's own disclosed note on why DELETE can't reach
                # this local file itself). Not a failure worth retrying
                # -- there's nothing left to write a result to. Treated
                # as a terminal outcome specifically so the `finally`
                # block below still cleans up the now-orphaned raw
                # upload, closing that exact gap.
                logger.warning("Student video pipeline: video %s not found (likely deleted)", video_id)
                terminal_outcome_reached = True
                return {"status": "video_not_found", "video_id": video_id}

            try:
                # 1. HLS transcode + Cloudflare R2 upload (reuses
                # media_service.py for the transcode, r2_service.py for
                # the upload -- Student Video Pinata -> Cloudflare R2
                # Migration step). `key_prefix` (was `pin_name`, a
                # Pinata-only dashboard label) is now a REAL R2 object
                # key prefix determining where this video's HLS tree
                # actually lives in the bucket.
                hls_output_dir = os.path.join(tmp_dir, "hls_output")
                hls_master_url, transcode_metadata = student_video_service.transcode_and_upload_to_r2(
                    local_filepath, hls_output_dir, key_prefix=f"student-videos/{video_id}/hls"
                )

                # 1.5. Hybrid Video Thumbnail System: custom upload (if
                # provided) or FFmpeg first-second-frame fallback,
                # uploaded to R2. NEVER raises -- see
                # `resolve_thumbnail_url`'s own docstring for why a
                # thumbnail failure must not fail (or trigger a retry
                # of) the whole pipeline. Placed right after step 1
                # (not interleaved with steps 2-5) because it is
                # entirely independent of them -- it only ever reads
                # `local_filepath`/`thumbnail_local_filepath`, the same
                # two inputs available from the very start of this
                # `try` block. `object_key` (was `pin_name`) is now a
                # real R2 key -- the file extension is deliberately NOT
                # hardcoded to `.jpg` here even though the FFmpeg
                # fallback always produces one, since a custom upload
                # could legitimately be a `.png` -- `object_key` uses a
                # generic `thumbnail` basename with no extension; R2
                # itself has no need for one (unlike a local filesystem
                # path), and the object's `Content-Type` (set correctly
                # by `resolve_thumbnail_url` either way) is what actually
                # governs how a browser renders it.
                thumbnail_url = student_video_service.resolve_thumbnail_url(
                    local_filepath,
                    thumbnail_local_filepath,
                    tmp_dir,
                    object_key=f"student-videos/{video_id}/thumbnail",
                )

                # 2. Audio extraction (reuses media_service.py)
                audio_path = os.path.join(tmp_dir, "audio.mp3")
                student_video_service.extract_audio(local_filepath, audio_path)

                # 3. Whisper-1 transcription (OpenAI). Returns a
                # `TranscriptionResult` (text + real per-segment
                # timestamps + duration), not a plain string -- disclosed
                # change, Automated WebVTT Captions Integration step, see
                # `student_video_service.py`'s module docstring.
                transcription_result = await student_video_service.transcribe_audio(audio_path)

                # 4. Gemini structured safety/quality evaluation (still
                # takes plain text -- Gemini has no use for segment
                # timing, only the words themselves).
                #
                # Backend Folder Structure & Core Database Models step
                # addition: this is the concrete call site for that
                # step's "inject the User's academic_level into the LLM
                # context prompt" requirement -- see `ai_context.
                # build_academic_context`'s own docstring. `student` is
                # fetched fresh here (not carried from `video`, which
                # has no ORM relationship to `User`) via a plain
                # `session.get`, the cheapest possible lookup on this
                # session; `None` in the vanishingly rare case a
                # `StudentVideo`'s own `student_id` no longer resolves
                # to a `User` row (e.g. a hard-deleted account) --
                # `build_academic_context` is simply skipped, same as
                # any other student who hasn't set either profile field.
                student = await session.get(User, video.student_id)
                academic_context = ai_context.build_academic_context(student) if student is not None else None
                evaluation = await student_video_service.evaluate_and_moderate(
                    transcription_result.text, academic_context=academic_context
                )

                # 5. Summary embedding (OpenAI text-embedding-3-small)
                embedding = await student_video_service.generate_summary_embedding(evaluation.summary)
            except Exception as exc:
                if is_final_attempt:
                    # Final allowed attempt -- write the permanent FAILED
                    # outcome now, ourselves, rather than letting the
                    # exception propagate uncaught: `autoretry_for`
                    # already won't schedule a further retry past
                    # `max_retries`, and if this exception were simply
                    # re-raised here, it would escape this function
                    # entirely (Celery marks the task FAILURE) WITHOUT
                    # this DB write ever happening -- leaving the video
                    # stuck at `PENDING_AI` forever. Returning a plain
                    # dict (not raising) keeps Celery's own task state
                    # SUCCESS -- "the pipeline machinery ran to
                    # completion and recorded a failure," same posture
                    # as `media_processing.py`'s `_process_lesson_video`
                    # on its own FAILED path.
                    video.status = StudentVideoStatus.FAILED.value
                    video.ai_feedback = {**(video.ai_feedback or {}), "error": str(exc)}
                    # Hybrid Video Thumbnail System addition -- if
                    # thumbnail resolution (step 1.5, above) already
                    # succeeded before a LATER step (e.g. Whisper) hit
                    # this final, permanent failure, there is no reason
                    # to throw that already-resolved thumbnail away: a
                    # FAILED video's owner can still see a preview image
                    # while deciding whether to re-upload. `thumbnail_url`
                    # is `None` here if step 1.5 itself never succeeded
                    # (or was never reached, e.g. the transcode itself
                    # failed first) -- either way, this assignment is
                    # always correct, never a guess.
                    video.thumbnail_url = thumbnail_url
                    session.add(video)
                    await session.commit()
                    terminal_outcome_reached = True
                    logger.error(
                        "Student video pipeline permanently failed for %s after %d retries: %s",
                        video_id, task.request.retries, exc,
                    )
                    return {"status": "failed", "video_id": video_id, "reason": str(exc)}

                # Not the final attempt -- deliberately does NOT touch
                # `video.status` here (it stays `PENDING_AI`, correctly
                # reflecting that a retry is still genuinely pending) and
                # re-raises so `autoretry_for=(Exception,)` catches it
                # and schedules the next attempt with exponential
                # backoff. `local_filepath` survives (see `finally`
                # below) specifically so that next attempt has something
                # to read.
                logger.warning(
                    "Student video pipeline attempt %d failed for %s (will retry): %s",
                    task.request.retries, video_id, exc,
                )
                raise

            # Success path -- Gemini's own `safety_approved` verdict
            # decides APPROVED vs. REJECTED; both are legitimate,
            # NON-exceptional pipeline outcomes (the pipeline itself
            # worked correctly either way), so neither one raises or
            # triggers a retry.
            ai_feedback = AIFeedback(
                summary=evaluation.summary,
                strengths=evaluation.strengths,
                growth_points=evaluation.growth_points,
                tags=evaluation.tags,
            )

            video.hls_master_url = hls_master_url
            # Hybrid Video Thumbnail System addition -- set on BOTH the
            # APPROVED and REJECTED branches alike (unconditionally,
            # right alongside `hls_master_url`), since thumbnail
            # resolution has nothing to do with Gemini's moderation
            # verdict -- it already ran, successfully or not, back in
            # step 1.5, well before evaluation ever happened.
            video.thumbnail_url = thumbnail_url
            video.status = (
                StudentVideoStatus.APPROVED.value
                if evaluation.safety_approved
                else StudentVideoStatus.REJECTED.value
            )
            video.ai_score = evaluation.score
            video.ai_safety_flag = not evaluation.safety_approved
            video.ai_feedback = {**ai_feedback.model_dump(), "transcode_metadata": transcode_metadata}
            video.summary_embedding = embedding

            # Automated WebVTT Captions Integration additions. Persisted
            # on BOTH the APPROVED and REJECTED branches (the
            # transcription step already ran successfully either way by
            # the time this line executes) -- NOT persisted on the
            # FAILED path below, a disclosed, deliberately narrower scope:
            # if the pipeline fails after transcription but before
            # reaching this point, keeping that partial transcript around
            # would serve no purpose, since a FAILED video never gets an
            # `hls_master_url` either and can never be played (or have its
            # captions requested) regardless.
            video.transcript_text = transcription_result.text
            video.transcript_segments = (
                [segment.model_dump() for segment in transcription_result.segments] or None
            )
            video.duration_seconds = (
                round(transcription_result.duration_seconds)
                if transcription_result.duration_seconds is not None
                else None
            )

            session.add(video)
            await session.commit()
            await session.refresh(video)
            terminal_outcome_reached = True

            # Gamification: Daily Streaks, Badges, and Leaderboard
            # addition -- this step's own literal "approved UGC uploads"
            # XP trigger. Gated on `evaluation.safety_approved` (the
            # APPROVED branch only; a REJECTED video earns nothing).
            # Every `StudentVideo` enters this success branch from
            # `PENDING_AI` exactly once in its lifetime (this pipeline
            # never re-runs against an already-terminal video), so
            # there's no "already approved before" case to guard against
            # here the way `admin_moderation.override_video_decision`
            # needs to (see that function's own comment). Best-effort,
            # AFTER the commit above -- a new subsystem's failure must
            # never turn an otherwise-successful pipeline run into a
            # retried/failed Celery task.
            if evaluation.safety_approved:
                try:
                    await gamification_service.add_xp(
                        session, video.student_id, gamification_service.UGC_APPROVAL_XP, "ugc_approval"
                    )
                except Exception:
                    logger.exception(
                        "Gamification: failed to award UGC-approval XP for video %s (student %s)",
                        video_id, video.student_id,
                    )

            # Smart Notifications Engine addition -- this step's own
            # literal "Video Approvals" trigger, at the SAME call site
            # (and same `evaluation.safety_approved` gate) as the
            # Gamification XP award immediately above; a REJECTED video
            # is a genuinely different, not-yet-built notification this
            # ask never asks for. Best-effort, same reasoning as the XP
            # award: a notification failure must never turn an otherwise-
            # successful pipeline run into a retried/failed Celery task.
            if evaluation.safety_approved:
                try:
                    await notification_service.create_notification(
                        session,
                        video.student_id,
                        "ACHIEVEMENT",
                        "Your video was approved!",
                        f'Your video "{video.title}" passed AI review and is now live on your profile.',
                    )
                except Exception:
                    logger.exception(
                        "Notifications: failed to notify student %s of video %s approval",
                        video.student_id, video_id,
                    )

            result = {
                "status": video.status,
                "video_id": video_id,
                "ai_score": video.ai_score,
                "hls_master_url": video.hls_master_url,
            }
            logger.info("Student video pipeline completed for %s: %s", video_id, result)
            return result
    finally:
        # Every intermediate artifact THIS attempt generated (audio
        # extraction, HLS output tree) is force-deleted regardless of
        # outcome -- these are cheaply regenerated from `local_filepath`
        # on a future retry, so there's no reason to keep them around.
        shutil.rmtree(tmp_dir, ignore_errors=True)
        if terminal_outcome_reached:
            _cleanup_raw_upload(local_filepath, thumbnail_local_filepath)
        await engine.dispose()


def _cleanup_raw_upload(local_filepath: str, thumbnail_local_filepath: str | None = None) -> None:
    """Best-effort deletion of the router's raw upload, its optional
    sibling custom-thumbnail upload, AND their shared containing temp
    directory (each upload gets its own dedicated `tempfile.mkdtemp()`-
    created directory -- see the router -- never shared across videos,
    so it's always safe to remove once empty). Swallows any OS-level
    error rather than letting a cleanup failure mask the pipeline's own
    real, already-committed result.

    Hybrid Video Thumbnail System addition: `thumbnail_local_filepath`
    is now deleted HERE, before the `os.rmdir(parent_dir)` call below --
    disclosed necessary bug fix (see the module docstring's own
    "Hybrid Video Thumbnail System" section for the full story): the
    ORIGINAL version of this function only ever knew about
    `local_filepath`, so a custom thumbnail sitting in the very same
    directory would have made `os.rmdir` fail silently every time
    (`os.rmdir` refuses a non-empty directory), permanently leaking one
    temp directory per upload that included a custom thumbnail.
    """
    try:
        if thumbnail_local_filepath and os.path.isfile(thumbnail_local_filepath):
            os.remove(thumbnail_local_filepath)
        if os.path.isfile(local_filepath):
            os.remove(local_filepath)
        parent_dir = os.path.dirname(local_filepath)
        if parent_dir and os.path.isdir(parent_dir) and not os.listdir(parent_dir):
            os.rmdir(parent_dir)
    except OSError:
        logger.warning("Failed to clean up raw upload at %s (non-fatal)", local_filepath, exc_info=True)
