"""Router layer for the Student Video Profiles & AI-Moderated Upload
System.

No shared `prefix` (same posture as `media_router.py`) -- the five paths
below don't share one common segment ("upload" and "search" sit under
`/student/videos/...`, the public feed sits under `/student/{student_id}/
videos`), so each route's full literal path is written directly in its
own decorator rather than forcing an artificial shared prefix.

Routing-precedence check performed for this router too (same discipline
as `media_router.py`'s `/courses/recommendations` vs. `/courses/{course_id}`
collision): `GET /student/{student_id}/videos` (segment 2 is the literal
"videos") and `GET /student/videos/search` / `GET /student/videos/{video_id}/feedback`
(segment 1 is the literal "videos") occupy DIFFERENT segment positions,
so there is no actual FastAPI/Starlette path-template ambiguity here the
way there was for `/courses/*` -- confirmed empirically anyway (see the
README's verification section) rather than just reasoned about.

Security Shield: Rate Limiting & Strict File Validation step (added after
the R2 migration and Video Analytics steps): `upload_student_video` below
now runs `check_upload_rate_limit` (`app/api/dependencies/rate_limiter.py`,
a 429-raising dependency, 3 uploads/24h rolling window) and
`validate_video_upload`/`validate_thumbnail_upload`
(`app/core/file_validator.py`, cursor-based size checks + strict MIME/
extension/double-extension checks) BEFORE any bytes are read or written
to local disk. This REPLACES the inline `_ALLOWED_CONTENT_TYPES`/
`_MAX_UPLOAD_SIZE_BYTES`/`_ALLOWED_THUMBNAIL_CONTENT_TYPES`/
`_ALLOWED_THUMBNAIL_EXTENSIONS`/`_MAX_THUMBNAIL_SIZE_BYTES` constants and
their inline `len(await file.read())`-based checks that used to live in
this module -- see `app/core/file_validator.py`'s own module docstring
for the disclosed narrowing this causes (webm/mkv video uploads, allowed
by the old inline check, are no longer accepted; `.avi` is newly accepted
instead, per this step's own literal, stricter spec).
"""

import logging
import os
import tempfile
import uuid

from fastapi import APIRouter, File, Form, HTTPException, Query, Response, UploadFile, status
from sqlalchemy import func, select

from app.api.dependencies.rate_limiter import CheckUploadRateLimit
from app.api.deps import CurrentUser, DbSession
from app.core.file_validator import validate_thumbnail_upload, validate_video_upload
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.schemas.opportunity import PaginatedResponse
from app.services import blacklist_service
from app.schemas.student_videos import (
    StudentVideoMyUploadOut,
    StudentVideoOut,
    StudentVideoPrivateFeedbackOut,
    StudentVideoSearchResult,
    StudentVideoUploadResponse,
)
from app.services.student_video_service import (
    CaptionsUnavailableError,
    VideoSearchError,
    generate_webvtt_captions,
    search_student_videos,
)
from app.tasks.student_video import process_student_video_pipeline_task
from app.workers.cleanup_tasks import task_delete_orphaned_media

logger = logging.getLogger(__name__)

router = APIRouter(tags=["student-videos"])


@router.post(
    "/student/videos/upload",
    response_model=StudentVideoUploadResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def upload_student_video(
    current_user: CurrentUser,
    db: DbSession,
    _rate_limit_check: CheckUploadRateLimit,
    title: str = Form(..., min_length=5, max_length=100),
    description: str | None = Form(default=None, max_length=5000),
    video: UploadFile = File(..., description="A presentation/project video file."),
    thumbnail: UploadFile | None = File(
        default=None,
        description="Optional custom thumbnail image (JPG/PNG, max 5MB). If omitted, a "
        "thumbnail is automatically extracted from the video's first second via FFmpeg.",
    ),
) -> StudentVideoUploadResponse:
    """Authenticated. Saves the raw upload to a secured local temporary
    directory (`tempfile.mkdtemp()` -- mode `0700`, owner-only, with a
    non-guessable generated directory name; "secured" in the sense of not
    being world-readable or enumerable on a shared host, NOT encryption
    at rest), creates the `StudentVideo` row at `PENDING_AI`, and
    dispatches `process_student_video_pipeline_task` -- per this step's
    own explicit component 5 router spec, essentially verbatim.

    Deliberate ordering: the file is written to disk BEFORE the DB row
    is created, not after. If the DB insert then failed, the result is
    an orphaned local temp file with no trace in the API -- a minor,
    silent resource leak. The reverse ordering (DB row first, then a
    failed file write) would instead leave a `PENDING_AI` row visibly
    stuck forever with no task ever dispatched against it -- a worse,
    user-facing failure mode requiring manual cleanup. See
    `app/tasks/student_video.py`'s module docstring for the related,
    larger disclosed trade-off: this whole local-filesystem-handoff
    design (rather than the Core Video Media Processing Pipeline's
    Supabase-Storage-mediated handoff) only works when the Celery worker
    shares a filesystem with this API process.

    --- Hybrid Video Thumbnail System additions, disclosed here ---

    Disclosed rename: this parameter was previously named `file`. This
    step's own literal ask names it `video` (`video: UploadFile =
    File(...)`), and -- verified directly, not assumed -- nothing in
    this codebase calls this endpoint yet (no frontend upload form
    existed before this step; the earlier Real-time Upload Tracker step
    only ever built the polling/read side, `GET /student/videos/my-uploads`).
    Renaming a genuinely uncalled parameter to match a later, more
    specific ask is a safe, disclosed cleanup, not a breaking change.
    The local ORM variable below is named `student_video` (not `video`)
    specifically to avoid shadowing this parameter within the same
    function scope.

    Disclosed judgment call on WHERE the actual thumbnail work happens:
    a custom thumbnail's bytes are validated and written to a local temp
    file HERE (cheap, synchronous, no external I/O), but the actual
    Pinata upload for it happens INSIDE the Celery task, not in this
    route handler -- the ask itself explicitly offers this as an
    alternative ("Upload this raw image file directly to Pinata IPFS
    immediately (**or pass its temporary path to the task**)"). Choosing
    the latter is a deliberate response to `media_service.py`'s own
    module docstring, which explains at length why its Pinata-calling
    functions are plain synchronous `httpx.Client` calls: that reasoning
    explicitly depends on their only ever being invoked from a Celery
    worker's own thread, NOT from inside an async FastAPI request
    handler sharing one event loop with every other in-flight request.
    Calling a blocking `httpx.Client` Pinata upload directly in THIS
    coroutine would stall the whole server for every other concurrent
    request for the duration of that upload -- so the raw thumbnail
    bytes are handed to the already-established Celery pipeline instead,
    exactly like the raw video bytes already are, keeping this route
    handler itself free of any blocking network I/O.

    Security Shield: Rate Limiting & Strict File Validation step -- both
    the rate-limit check (`_rate_limit_check`, a dependency; see
    `app/api/dependencies/rate_limiter.py`) and the file validators below
    (`app/core/file_validator.py`) now run BEFORE any bytes are read from
    either upload or written to local disk. `validate_video_upload`/
    `validate_thumbnail_upload` use `file.file.seek(0, 2)`/`.tell()`/
    `.seek(0)` cursor manipulation to determine each file's size WITHOUT
    reading it fully into memory first -- a real improvement over this
    endpoint's own PRIOR inline checks (superseded, removed from this
    module), which read the entire file into memory via `await file.read()`
    just to call `len(...)` on the result, for both the (up to 2GB) video
    and the (up to 5MB) thumbnail alike. `await video.read()`/`await
    thumbnail.read()` below still happen (this endpoint's own local-disk-
    staging design, see the module docstring, hasn't changed), but only
    AFTER both files have already passed validation -- and the cursor
    reset inside each validator (`file.file.seek(0)`) is exactly what
    makes those subsequent `.read()` calls still work correctly.

    Validation ordering: the thumbnail (if present) is validated BEFORE
    the video. Both validation calls themselves are now O(1) (a cursor
    seek, not a full read), so this ordering is no longer about "cheap
    check first" the way it used to be -- it's kept purely so a bad
    thumbnail is still rejected before the (potentially much larger)
    video's bytes are ever read into memory and written to disk, the
    same "don't do expensive work you're about to throw away" reasoning
    as before, just now applying to the READ+WRITE phase rather than the
    validation phase itself.
    """
    # General Content Blacklist / Pattern Auto-Trigger addition -- runs
    # FIRST, before any file bytes are even validated/read: a pure text
    # check against `title`/`description`, the cheapest possible check
    # in this whole handler, so a blacklisted title fails fast without
    # ever touching the (potentially large) video upload. Checks BOTH
    # fields independently (a blacklist hit in either one blocks the
    # whole upload) -- `check_content` itself already treats a `None`
    # `description` as "no match", so this is safe when no description
    # was supplied.
    for field_name, field_value in (("title", title), ("description", description)):
        blacklist_match = await blacklist_service.check_content(db, field_value)
        if blacklist_match is not None:
            blacklist_service.log_trigger(
                db,
                submitting_user_id=current_user.id,
                match=blacklist_match,
                content_type=f"student_video_{field_name}",
                content_snippet=field_value or "",
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail=f"This video's {field_name} can't be published -- it contains content that isn't allowed here.",
            )

    if thumbnail is not None:
        validate_thumbnail_upload(thumbnail)
    validate_video_upload(video)

    thumbnail_bytes: bytes | None = None
    thumbnail_ext = ""
    if thumbnail is not None:
        _, thumbnail_ext = os.path.splitext(thumbnail.filename or "")
        thumbnail_ext = thumbnail_ext.lower()
        thumbnail_bytes = await thumbnail.read()

    video_bytes = await video.read()

    # Both the raw video AND (if present) the custom thumbnail live as
    # SIBLING files in this same per-upload temp directory -- deliberate,
    # so a single `_cleanup_raw_upload` call (app/tasks/student_video.py)
    # can remove the whole directory once both files are gone, rather
    # than tracking two independent temp directories with two lifecycles.
    upload_dir = tempfile.mkdtemp(prefix="student_video_upload_")
    _, ext = os.path.splitext(video.filename or "")
    local_filepath = os.path.join(upload_dir, f"raw{ext or '.mp4'}")
    with open(local_filepath, "wb") as f:
        f.write(video_bytes)

    thumbnail_local_filepath: str | None = None
    if thumbnail_bytes is not None:
        thumbnail_local_filepath = os.path.join(upload_dir, f"custom_thumbnail{thumbnail_ext}")
        with open(thumbnail_local_filepath, "wb") as f:
            f.write(thumbnail_bytes)

    student_video = StudentVideo(
        student_id=current_user.id,
        title=title,
        description=description,
        status=StudentVideoStatus.PENDING_AI.value,
    )
    db.add(student_video)
    await db.commit()
    await db.refresh(student_video)

    async_result = process_student_video_pipeline_task.delay(
        str(student_video.id), local_filepath, thumbnail_local_filepath
    )

    return StudentVideoUploadResponse(
        video_id=student_video.id, task_id=async_result.id, status=student_video.status
    )


@router.get("/student/videos/my-uploads", response_model=list[StudentVideoMyUploadOut])
async def list_my_uploaded_videos(current_user: CurrentUser, db: DbSession) -> list[StudentVideoMyUploadOut]:
    """Real-time Student UGC Video Upload Tracker & Feedback Dashboard
    step. Authenticated, owner-only -- returns EVERY video this student
    has ever uploaded, regardless of status (unlike the public feed,
    which filters to `APPROVED` only), since the whole point of this
    endpoint is letting the student see `PENDING_AI`/`REJECTED`/`FAILED`
    uploads too.

    Routing-precedence check performed (same discipline as every other
    router in this codebase): "my-uploads" and the path-parameter
    `{video_id}` used by `DELETE /student/videos/{video_id}` occupy the
    SAME segment position, but there's no actual collision here --
    FastAPI/Starlette routes are matched per-HTTP-method independently,
    and no OTHER `GET` route in this file matches a single bare segment
    after "videos" (the closest, `GET /student/videos/{video_id}/feedback`,
    has an extra trailing segment). Confirmed empirically anyway (see the
    README's verification section).

    Performance note, per this step's own explicit "leverage the
    composite index" instruction: `ix_student_videos_student_id_status`
    is a `(student_id, status)` B-tree index. A `WHERE student_id = ?`
    query with no `status` filter (this one) still uses that index
    efficiently via its leading column -- a composite index doesn't
    require filtering on every one of its columns to be useful, only a
    left-to-right prefix of them. Disclosed honestly, though: the
    `ORDER BY created_at DESC` this step also asks for is NOT covered by
    that same index (it's `(student_id, status)`, not `(student_id,
    created_at)`) -- Postgres will use the composite index to find this
    student's rows quickly, then sort the (typically small, one
    student's own uploads) result set separately. A true covering index
    for this exact query shape would be `(student_id, created_at)`, not
    the `(student_id, status)` one this step's ask itself names -- adding
    a second index was judged unnecessary scope for what is, per row
    count, an inherently small per-student result set.
    """
    result = await db.execute(
        select(StudentVideo)
        .where(StudentVideo.student_id == current_user.id)
        .order_by(StudentVideo.created_at.desc())
    )
    videos = result.scalars().all()
    return [StudentVideoMyUploadOut.model_validate(v) for v in videos]


@router.get("/student/{student_id}/videos", response_model=PaginatedResponse[StudentVideoOut])
async def list_student_videos(
    student_id: uuid.UUID,
    db: DbSession,
    limit: int = Query(default=20, ge=1, le=100),
    offset: int = Query(default=0, ge=0),
) -> PaginatedResponse[StudentVideoOut]:
    """Public route -- no auth dependency at all, per the ask's own
    "Public route" wording. Returns only `APPROVED` videos (a
    `PENDING_AI`/`REJECTED`/`FAILED` video is never shown on a public
    profile feed -- disclosed necessary masking, same reasoning as every
    other premium/moderated-content gate in this codebase). Reuses
    `PaginatedResponse` (app/schemas/opportunity.py) rather than a second,
    near-identical generic pagination wrapper -- it's already a generic
    `PaginatedResponse[T]`, built for exactly this reuse.
    """
    filters = [StudentVideo.student_id == student_id, StudentVideo.status == StudentVideoStatus.APPROVED.value]

    total = (
        await db.execute(select(func.count()).select_from(StudentVideo).where(*filters))
    ).scalar_one()

    result = await db.execute(
        select(StudentVideo).where(*filters).order_by(StudentVideo.created_at.desc()).limit(limit).offset(offset)
    )
    videos = result.scalars().all()

    return PaginatedResponse[StudentVideoOut](items=videos, total=total, limit=limit, offset=offset)


@router.get("/student/videos/{video_id}/feedback", response_model=StudentVideoPrivateFeedbackOut)
async def get_student_video_feedback(
    video_id: uuid.UUID, current_user: CurrentUser, db: DbSession
) -> StudentVideoPrivateFeedbackOut:
    """Secure route. 404 if the video doesn't exist at all; 403 (per the
    ask's own explicit instruction) -- not 404 -- if it exists but
    belongs to someone else. Unlike this codebase's usual "404 for both"
    owned-resource posture (see `DELETE` below), this endpoint's ask
    literally specifies "Otherwise, raises 403 Forbidden," so that's
    honored as stated rather than silently normalized to the other
    convention.
    """
    video = await db.get(StudentVideo, video_id)
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student video not found")
    if current_user.id != video.student_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="You do not have permission to view this video's feedback.",
        )
    return StudentVideoPrivateFeedbackOut.model_validate(video)


@router.get("/student/videos/{video_id}/captions.vtt")
async def get_student_video_captions(video_id: uuid.UUID, db: DbSession) -> Response:
    """Automated WebVTT Captions Integration. Public route -- no auth
    dependency, same posture as `GET /student/{student_id}/videos` --
    but gated to `APPROVED` videos only, for the identical reason that
    endpoint filters to `APPROVED`: a `PENDING_AI`/`REJECTED`/`FAILED`
    video's transcript should not leak through a side-channel endpoint
    just because its captions track happens to have its own URL. This
    also exactly mirrors `IPFSVideoPlayer.tsx`'s own gating -- the video
    source itself never loads for a non-`APPROVED` video either, so a
    caption track for one would be pointless even if it weren't blocked
    here.

    404 (not a 403, and not an empty-but-200 response) for every
    "nothing to show" case alike -- video doesn't exist, exists but isn't
    `APPROVED` yet, or is `APPROVED` but has no transcript data at all
    (a row from before the Automated WebVTT Captions Integration step,
    or the rare case where transcription produced zero usable segments).
    Collapsing "not approved" and "not found" into the same 404 mirrors
    the public list endpoint's own approved-only masking; collapsing "no
    transcript data" into that same 404 (via `CaptionsUnavailableError`)
    means a caller seeing 404 never has to distinguish "this video will
    never have captions" from "this video doesn't exist" -- neither case
    is actionable differently from a `<track>` element's own point of
    view (browsers silently ignore a track whose `src` 404s).

    Returns via `Response`, not `PlainTextResponse` -- `PlainTextResponse`
    hardcodes `media_type="text/plain"` unless overridden, and this
    route needs the exact `media_type="text/vtt; charset=utf-8"`
    content-type WebVTT requires; `Response` takes that media type
    directly with no default to override. The body is passed as
    already-UTF-8-encoded `bytes` (not the raw `str`) specifically so
    Arabic caption text round-trips correctly regardless of Starlette's
    default response encoding behavior.
    """
    video = await db.get(StudentVideo, video_id)
    if video is None or video.status != StudentVideoStatus.APPROVED.value:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Captions not available for this video")

    try:
        vtt_content = generate_webvtt_captions(
            segments=video.transcript_segments,
            transcript_text=video.transcript_text,
            duration_seconds=video.duration_seconds,
        )
    except CaptionsUnavailableError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Captions not available for this video")

    return Response(content=vtt_content.encode("utf-8"), media_type="text/vtt; charset=utf-8")


@router.delete("/student/videos/{video_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_student_video(video_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> None:
    """Owner-only deletion. Uses this codebase's established
    `_get_owned_<resource>_or_404` posture (query filtered on `id` AND
    `student_id` together, so a video belonging to someone else 404s
    exactly like one that doesn't exist -- see e.g.
    `budget_service.delete_budget`) -- a deliberate, disclosed departure
    from `get_student_video_feedback`'s explicit 403, since THIS
    endpoint's own ask doesn't specify a status code for the
    wrong-owner case, leaving this codebase's own uniform convention as
    the right default.

    "Schedules a cleanup of resources if needed," per the ask: no
    additional action is scheduled here, and that's a considered
    conclusion, not an omission. If the pipeline already reached a
    terminal state (`APPROVED`/`REJECTED`/`FAILED`), the Celery task's
    own strict `try/finally` (app/tasks/student_video.py) has already
    force-deleted every local file for this video by the time this
    endpoint could ever be called against it. If the video is still
    `PENDING_AI` (the task hasn't finished, or hasn't even started yet),
    this endpoint has no path to that task's local file at all --
    `local_filepath` is never persisted anywhere in the DB (see
    `StudentVideo`'s own field list) -- but the task itself, when it
    eventually runs and finds this row gone, treats that as a terminal
    outcome and cleans up the now-orphaned raw upload itself (see that
    task's `video is None` branch).

    Cloud Garbage Collection (IPFS Unpinning & R2 Deletion) step,
    DISCLOSED UPDATE TO THIS DOCSTRING'S OWN PRIOR CLAIM: this paragraph
    used to end here, on a since-outdated note that "the one resource
    genuinely left behind by design is an already-pinned IPFS CID for a
    since-deleted APPROVED video -- never explicitly unpinned." That was
    true when it was written, but predates the Student Video Pinata ->
    Cloudflare R2 Migration step -- `StudentVideo` has had no IPFS CID
    columns at all since then, only `hls_master_url`/`thumbnail_url`
    (Cloudflare R2 objects). This step closes that gap for its CURRENT
    (R2) form: `video.hls_master_url`/`video.thumbnail_url` are captured
    BEFORE the row is deleted, and `task_delete_orphaned_media`
    (`app/workers/cleanup_tasks.py`) is dispatched fire-and-forget with
    whichever of the two is non-`None` -- a `PENDING_AI` video that never
    finished transcoding legitimately has neither URL set yet, in which
    case nothing is dispatched at all (there is nothing in R2 to clean up
    for it).

    DISCLOSED ORDERING CHOICE, a deliberate departure from processing
    this feature's own literal step-by-step instruction ("extract urls,
    dispatch the task, THEN delete the row") in that exact order: this
    handler instead captures the URLs, deletes the row, commits, and ONLY
    THEN dispatches the cleanup task. Dispatching before a successful
    commit would mean a real object could be deleted from R2 for a
    database delete that, however unlikely for a single-row
    delete-by-primary-key, still failed or rolled back -- leaving the DB
    row referencing storage that no longer exists. Dispatching only after
    a confirmed commit makes "the DB row is really gone" the actual
    trigger for "the storage may now be cleaned up," the stronger, more
    correct invariant; the Celery dispatch itself is still fire-and-forget
    and does not block this endpoint's response either way, so this
    feature's own core requirement (don't make the caller wait on cloud
    deletion) is fully met regardless of which side of the commit the
    dispatch sits on.
    """
    result = await db.execute(
        select(StudentVideo).where(StudentVideo.id == video_id, StudentVideo.student_id == current_user.id)
    )
    video = result.scalar_one_or_none()
    if video is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Student video not found")

    r2_urls = [url for url in (video.hls_master_url, video.thumbnail_url) if url]

    await db.delete(video)
    await db.commit()

    if r2_urls:
        task_delete_orphaned_media.delay(r2_urls=r2_urls)

    await db.delete(video)
    await db.commit()


@router.get("/student/videos/search", response_model=list[StudentVideoSearchResult])
async def search_student_videos_endpoint(
    db: DbSession,
    q: str = Query(..., min_length=1, max_length=500, description="Natural-language search query."),
    limit: int = Query(default=10, ge=1, le=50),
) -> list[StudentVideoSearchResult]:
    """Public route. Computes `q`'s embedding and performs a pgvector
    cosine-similarity search against `summary_embedding`, restricted to
    `APPROVED` videos -- see `student_video_service.search_student_videos`
    for the actual query and its own disclosed reasoning (WHERE-vs-
    ORDER-BY alias handling, the approved-only filter, threshold reuse).
    """
    try:
        return await search_student_videos(db, q, limit=limit)
    except VideoSearchError:
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Search is temporarily unavailable -- embedding generation failed.",
        )
