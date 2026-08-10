from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


# ---------------------------------------------------------------------
# Content authoring (disclosed necessary plumbing -- see
# course_service.py's "Content authoring" section docstring)
# ---------------------------------------------------------------------


class CourseCreateRequest(BaseModel):
    """POST /courses request body. Admin-only (`current_user.is_superuser`
    -- see the router)."""

    title: str = Field(min_length=1, max_length=255)
    description: str | None = Field(default=None, max_length=10_000)
    instructor_name: str = Field(default="AI Scholar Guide", max_length=255)
    token_unlock_cost: int = Field(default=0, ge=0)
    thumbnail_url: str | None = Field(default=None, max_length=1000)


class ModuleCreateRequest(BaseModel):
    """POST /courses/{course_id}/modules request body. Admin-only."""

    title: str = Field(min_length=1, max_length=255)
    order_index: int | None = Field(default=None, ge=0)


class LessonCreateRequest(BaseModel):
    """POST /courses/modules/{module_id}/lessons request body.
    Admin-only. `content_type` stays a plain `str` (not a `Literal`) --
    the ask writes it with "e.g." (video, text, pdf), non-exhaustive
    wording, same posture as `parent_pitch.primary_concern`."""

    title: str = Field(min_length=1, max_length=255)
    content_type: str = Field(min_length=1, max_length=50)
    video_url: str | None = Field(default=None, max_length=1000)
    text_content: str | None = Field(default=None, max_length=100_000)
    order_index: int | None = Field(default=None, ge=0)


class ModuleOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    course_id: UUID
    title: str
    order_index: int


class LessonOut(BaseModel):
    """Admin-facing (`POST .../lessons` response) -- unmasked, since the
    caller creating the lesson is by definition an admin. The HLS fields
    below are always `None`/unset here: they're only ever populated
    later, by the Core Video Media Processing Pipeline's Celery task,
    never at creation time."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    module_id: UUID
    title: str
    content_type: str
    video_url: str | None
    text_content: str | None
    order_index: int
    hls_master_cid: str | None
    # Hybrid Video Thumbnail System addition -- `from_attributes=True`
    # reads this straight off the `Lesson` ORM object, same as every
    # other field here; always `None` in this pass (see this class's
    # module-level context: no admin write path populates it yet).
    thumbnail_cid: str | None
    duration_seconds: int | None
    video_processing_status: str | None
    video_metadata: dict | None


# ---------------------------------------------------------------------
# Listing / detail (per the ask's own two GET endpoints)
# ---------------------------------------------------------------------


class CourseOut(BaseModel):
    """`GET /courses/` response item -- catalog view, no per-user
    progress annotation (that's `CourseDetailOut`, below, for `GET
    /courses/{id}`)."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None
    instructor_name: str
    token_unlock_cost: int
    thumbnail_url: str | None
    # Hybrid Video Thumbnail System addition -- separate from
    # `thumbnail_url` above (see app/models/courses.py's own docstring,
    # point 5, for why these are two distinct columns). `None` for every
    # course in this pass -- no admin upload path populates it yet, only
    # exposed here so the frontend's `CourseCard.tsx` has a stable field
    # to read once one exists.
    thumbnail_cid: str | None
    created_at: datetime


class LessonProgressOut(BaseModel):
    """One lesson within `CourseDetailOut`'s module list. `video_url`/
    `text_content`/`hls_master_cid`/`video_metadata` are all `None` for a
    premium, not-yet-enrolled course -- see
    `course_service.get_course_detail_by_object`'s docstring on why
    (built from a plain dict, not `from_attributes`, since the masking
    has to happen before this schema is constructed).

    `duration_seconds`/`video_processing_status` are deliberately NOT
    masked -- see the same function's docstring: knowing a preview's
    length or that it's still processing doesn't leak the premium
    content itself, and lets a non-enrolled browsing student see "12:34,
    ready to watch" as harmless catalog-preview info."""

    id: UUID
    title: str
    content_type: str
    video_url: str | None
    text_content: str | None
    order_index: int
    is_completed: bool
    hls_master_cid: str | None
    # Hybrid Video Thumbnail System addition -- see `CourseOut.thumbnail_cid`
    # above for the same reasoning; not masked for a non-enrolled premium
    # course, same posture as `duration_seconds`/`video_processing_status`
    # just below (a thumbnail image doesn't leak the premium content
    # itself any more than a duration does).
    thumbnail_cid: str | None
    duration_seconds: int | None
    video_processing_status: str | None
    video_metadata: dict | None


class ModuleDetailOut(BaseModel):
    id: UUID
    title: str
    order_index: int
    lessons: list[LessonProgressOut]


class CourseDetailOut(BaseModel):
    """`GET /courses/{id}` response -- "course details (modules,
    lessons, and enrollment/completion progress)" per the ask's own
    literal wording. Built from a plain dict returned by
    `course_service.get_course_detail`, not `from_attributes`."""

    id: UUID
    title: str
    description: str | None
    instructor_name: str
    token_unlock_cost: int
    thumbnail_url: str | None
    created_at: datetime
    is_enrolled: bool
    modules: list[ModuleDetailOut]


class RecommendedCourseOut(CourseDetailOut):
    """`GET /courses/recommendations` response item. Extends
    `CourseDetailOut` (same full modules/lessons shape, same masking
    rules for a non-enrolled premium course) with the two ranking fields
    `recommendation_service.get_recommended_courses` annotates each
    result dict with.

    Both are `None` together, as a pair, specifically for the
    "brand-new student, no history, no enrollments yet" fallback path
    (`get_user_interest_vector` returns `None`) -- that response is a
    plain "newest courses" list with no similarity computation behind
    it, so reporting a fake distance/score for it would be misleading.
    """

    cosine_distance: float | None = Field(
        default=None,
        description="Raw pgvector cosine distance (`<=>`) between the "
        "user's interest vector and this course's embedding, in [0, 2]. "
        "Lower is more similar. `None` for the non-personalized "
        "fallback ordering.",
    )
    relevance_score: float | None = Field(
        default=None,
        description="`1 - cosine_distance`, i.e. cosine similarity, "
        "rounded to 4 decimal places -- higher is more relevant. "
        "`None` for the non-personalized fallback ordering.",
    )


class StreamManifestOut(BaseModel):
    """`GET /lessons/{lesson_id}/stream-manifest` response --
    enrolled-students-only (see `media_router.py`'s
    `require_lesson_enrollment` dependency). Deliberately a SEPARATE,
    narrower schema from `LessonProgressOut` rather than reusing it: a
    video player only needs the playback-relevant fields, not the
    lesson's title/order_index/completion state, and `manifest_url` is
    a value that doesn't exist on the ORM model at all -- it's
    constructed by the router from `hls_master_cid` +
    `settings.pinata_gateway_url` at request time (see
    `media_router.py`'s `stream_manifest` endpoint), not stored.
    """

    lesson_id: UUID
    video_processing_status: str | None
    hls_master_cid: str | None = Field(
        default=None,
        description="Root IPFS CID of this lesson's HLS output folder. "
        "`None` until `video_processing_status` reaches `completed`.",
    )
    manifest_url: str | None = Field(
        default=None,
        description="Fully-qualified URL to this lesson's HLS master "
        "playlist (`{pinata_gateway_url}/{hls_master_cid}/master.m3u8`), "
        "ready to hand directly to an HLS-capable video player "
        "(hls.js, AVPlayer, ExoPlayer, etc.). `None` until "
        "`hls_master_cid` is set.",
    )
    duration_seconds: int | None
    video_metadata: dict | None


# ---------------------------------------------------------------------
# Enrollment / lesson completion / AI features
# ---------------------------------------------------------------------


class EnrollOut(BaseModel):
    """POST /courses/{id}/enroll response.

    Disclosed shape change, Production Token Gating & Enrollment Engine
    step: this previously read `{enrolled: bool, tokens_balance: int |
    None}`. That step's ask specifies a literal `{success, message,
    current_tokens}` envelope instead -- honored directly here rather
    than bolted on alongside the old field names, since there is exactly
    one production caller of this route today (the new
    `IPFSVideoPlayer.tsx` unlock flow being wired up in this same step;
    nothing in this codebase previously called it from a frontend), so
    there's no real existing integration this breaks.
    """

    success: bool
    message: str
    current_tokens: int | None = Field(
        default=None,
        description="Student's token balance after this call. Always populated "
        "on success (even for a free, zero-cost course, so the frontend can "
        "still refresh its displayed balance); null on failure.",
    )


class CompleteLessonOut(BaseModel):
    """POST /courses/lessons/{lesson_id}/complete response."""

    lesson_id: UUID
    already_completed: bool
    tokens_earned: int
    tokens_balance: int
    congratulations_message: str


class CopilotAskRequest(BaseModel):
    """POST /courses/lessons/{lesson_id}/copilot-ask request body."""

    student_question: str = Field(min_length=1, max_length=2000)


class CopilotAskOut(BaseModel):
    """POST /courses/lessons/{lesson_id}/copilot-ask response."""

    lesson_id: UUID
    answer: str


class QuizQuestionOut(BaseModel):
    """One question in `GET /courses/lessons/{lesson_id}/quiz`'s
    response. Includes `correct_answer_index`/`explanation` directly --
    disclosed choice: the ask's router section names no separate
    submit/grade endpoint for this quiz (unlike Focus Sessions' quiz,
    which has one), so this is a self-check study tool, not a scored
    assessment -- the student needs the answer to actually self-assess."""

    question: str
    options: list[str]
    correct_answer_index: int
    explanation: str
