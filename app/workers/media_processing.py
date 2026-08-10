import asyncio
import logging
import os
import shutil
import tempfile
import uuid

from app.core.celery_app import celery_app
from app.db.session import AsyncSessionLocal, engine
from app.models.courses import Lesson
from app.models.enums import VideoProcessingStatus
from app.services import media_service
from app.services.storage_service import StorageDeleteError, storage_service

logger = logging.getLogger(__name__)


@celery_app.task(name="app.workers.media_processing.process_lesson_video")
def process_lesson_video(lesson_id: str, raw_storage_path: str) -> dict:
    """Celery entrypoint (sync). Same `asyncio.run()` bridge as
    `app.workers.scholar_agent.run_autonomous_scholar_search` -- see that
    task's docstring for the full event-loop-lifetime reasoning behind
    the pattern this mirrors.

    `lesson_id` is a `str`, not a `UUID` -- Celery's configured
    `task_serializer="json"` (app/core/celery_app.py) can't serialize a
    raw `UUID`, same established precedent as `run_autonomous_scholar_search`'s
    `user_id: str` parameter. `raw_storage_path` is the Supabase Storage
    path the raw upload was written to by `media_router.py`'s
    `upload_video` endpoint (see `storage_service.upload_raw_bytes`) --
    passing a small string path here, not the video's actual bytes, is
    the whole point of that intermediate Storage handoff (see
    `storage_service.py`'s "Raw video handoff" section docstring for why
    a Celery task argument can't carry a multi-hundred-megabyte payload).
    """
    return asyncio.run(_process_lesson_video(lesson_id, raw_storage_path))


async def _process_lesson_video(lesson_id: str, raw_storage_path: str) -> dict:
    """The actual pipeline, async so it can reuse this codebase's normal
    `AsyncSession`/model-based DB access for its status updates (see
    `app/db/session.py`) rather than maintaining a second, parallel sync
    ORM code path just for this one task.

    Everything between the initial `PENDING`/lookup and the final DB
    write -- downloading the raw upload, `ffprobe`, `ffmpeg`, and the
    Pinata pin -- is itself fully SYNCHRONOUS work (`media_service.py` is
    a deliberately synchronous module; see its own docstring for why),
    called directly from this `async def` function. That's a legitimate,
    deliberate choice, not an inconsistency: an `async def` function is
    free to call blocking code, it just means THIS coroutine blocks
    while that code runs -- which is exactly fine here, since this
    coroutine is the only thing running inside this `asyncio.run()` call
    for the lifetime of this one Celery task execution. There is no
    second coroutine on this event loop that blocking would starve, the
    way it would inside a FastAPI request handler serving many
    concurrent requests on one shared loop. Cross-video concurrency is
    provided by Celery's own worker pool (multiple worker
    processes/threads, each running one task at a time), not by
    anything inside this function.

    Single top-level `try/finally` (mirroring
    `_run_autonomous_scholar_search`'s) that both disposes the shared
    async engine's connection pool AND removes this task's local temp
    directory, regardless of how the pipeline below exits.
    """
    tmp_dir = tempfile.mkdtemp(prefix="lesson_video_")
    try:
        async with AsyncSessionLocal() as session:
            lesson = await session.get(Lesson, uuid.UUID(lesson_id))
            if lesson is None:
                logger.warning("Media processing: lesson %s not found", lesson_id)
                return {"status": "lesson_not_found", "lesson_id": lesson_id}

            lesson.video_processing_status = VideoProcessingStatus.PROCESSING.value
            session.add(lesson)
            await session.commit()

            try:
                raw_bytes = await storage_service.download_raw_bytes(raw_storage_path)

                _, ext = os.path.splitext(raw_storage_path)
                input_path = os.path.join(tmp_dir, f"input{ext or '.mp4'}")
                with open(input_path, "wb") as f:
                    f.write(raw_bytes)

                duration_seconds = media_service.probe_video_duration(input_path)

                output_dir = os.path.join(tmp_dir, "hls_output")
                transcode_result = media_service.transcode_to_hls(input_path, output_dir)

                root_cid = media_service.upload_hls_directory_to_pinata(
                    output_dir, pin_name=f"lesson_{lesson_id}_hls"
                )
            except Exception as exc:
                # Broad, deliberate catch-all: `media_service`/
                # `storage_service` already raise their own specific
                # exception types (`HLSTranscodeError`, `PinataUploadError`,
                # `StorageDownloadError`) for expected failure modes, but
                # this task must NEVER leave a lesson stuck at
                # `PROCESSING` forever because of some unanticipated
                # error (a corrupt upload, a disk-full temp dir, etc.) --
                # every failure path, known or not, ends in a clean
                # `FAILED` status with the error message preserved for
                # an admin to see, not an unhandled exception that
                # crashes the Celery worker and leaves the lesson's
                # state ambiguous.
                logger.exception("Media processing failed for lesson %s", lesson_id)
                lesson.video_processing_status = VideoProcessingStatus.FAILED.value
                lesson.video_metadata = {**(lesson.video_metadata or {}), "error": str(exc)}
                session.add(lesson)
                await session.commit()
                return {"status": "failed", "lesson_id": lesson_id, "reason": str(exc)}

            lesson.hls_master_cid = root_cid
            lesson.duration_seconds = duration_seconds
            lesson.video_processing_status = VideoProcessingStatus.COMPLETED.value
            lesson.video_metadata = {**(lesson.video_metadata or {}), **transcode_result}
            session.add(lesson)
            await session.commit()
            await session.refresh(lesson)

            # Best-effort cleanup of the raw (pre-transcode) upload, now
            # that its HLS output is safely pinned -- non-fatal if this
            # fails, the pipeline has already succeeded from the
            # student's point of view either way.
            try:
                await storage_service.delete_raw_bytes(raw_storage_path)
            except StorageDeleteError:
                logger.warning(
                    "Failed to delete raw video upload at %s after successful processing (non-fatal)",
                    raw_storage_path,
                )

            result = {
                "status": "completed",
                "lesson_id": lesson_id,
                "hls_master_cid": root_cid,
                "duration_seconds": duration_seconds,
            }
            logger.info("Media processing completed for lesson %s: %s", lesson_id, result)
            return result
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        await engine.dispose()
