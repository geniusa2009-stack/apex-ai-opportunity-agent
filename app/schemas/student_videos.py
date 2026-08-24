from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.models.enums import StudentVideoStatus


class StudentVideoCreate(BaseModel):
    """`POST /student/videos/upload` form fields (alongside the uploaded
    file itself -- see the router, which reads this off `Form(...)`
    fields, not a JSON body, since the request is `multipart/form-data`).
    """

    title: str = Field(min_length=5, max_length=100)
    description: str | None = Field(default=None, max_length=5000)


class StudentVideoUploadResponse(BaseModel):
    """`POST /student/videos/upload` response -- same "return the
    dispatched Celery task_id" shape as `scholar_agent.py`'s
    `TriggerScholarAgentResponse` / the Core Video Media Processing
    Pipeline's `UploadVideoResponse`, plus the new row's id and its
    freshly-set `PENDING_AI` status so the client has everything needed
    to start polling without a second round-trip.
    """

    video_id: UUID
    task_id: str
    status: str


class StudentVideoOut(BaseModel):
    """Public view of an `APPROVED` student video -- exactly the fields
    the ask names for it: id, student_id, title, description,
    hls_master_url, created_at. Deliberately excludes `ai_score`/
    `ai_safety_flag`/`ai_feedback`/`summary_embedding` -- those are the
    owner-exclusive fields `StudentVideoPrivateFeedbackOut` adds below,
    and `summary_embedding` specifically (a 1536-float vector) has no
    client-side use in any response, public or private, same posture as
    `OpportunityOut` excluding `Opportunity.embedding`.

    `hls_master_url`/`thumbnail_url` RENAMED from `hls_master_cid`/
    `thumbnail_cid` (Student Video Pinata -> Cloudflare R2 Migration
    step) -- both now hold a full, directly-playable Cloudflare R2
    public URL rather than an IPFS CID. See `app/models/student_videos.py`'s
    module docstring for the full disclosed rationale.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    student_id: UUID
    title: str
    description: str | None
    hls_master_url: str | None
    # Hybrid Video Thumbnail System addition -- `None` until either a
    # custom-uploaded image or an FFmpeg-extracted fallback frame has
    # been resolved and uploaded (see `student_video_service.
    # resolve_thumbnail_url`); a card rendering this video simply falls
    # back to a placeholder/gradient while it's `None`, same posture as
    # `hls_master_url` being `None` during `PENDING_AI`.
    thumbnail_url: str | None
    created_at: datetime


class StudentVideoSearchResult(StudentVideoOut):
    """`GET /student/videos/search` response item -- `StudentVideoOut`
    plus the similarity score, same shape as `OpportunitySearchResult`
    extending `OpportunityOut` (app/schemas/opportunity.py)."""

    similarity_score: float = Field(
        description="Cosine similarity between the search query's embedding "
        "and this video's summary_embedding, roughly in [0, 1] for typical "
        "text embeddings (1 = identical direction). Higher is more relevant."
    )


class AIFeedback(BaseModel):
    """The structured shape `ai_feedback` (JSONB) is validated against
    before being persisted -- exactly the four keys and constraints this
    step's own Gemini system prompt specifies. Used both by
    `student_video_service.py` (to validate Gemini's structured output
    before writing it to the DB) and nested directly inside
    `StudentVideoPrivateFeedbackOut` below (so the owner-facing API
    response is validated against this same shape, not a loose `dict`).
    """

    summary: str = Field(description="A 2-sentence, highly engaging summary of the topic discussed.")
    strengths: list[str] = Field(min_length=2, description="At least 2 strong points about the explanation.")
    growth_points: list[str] = Field(
        min_length=2, description="At least 2 constructive tips for improvement."
    )
    tags: list[str] = Field(min_length=3, max_length=5, description="3-5 relevant educational tags.")


class StudentVideoPrivateFeedbackOut(StudentVideoOut):
    """`GET /student/videos/{video_id}/feedback` response -- "the public
    fields PLUS ai_score, ai_safety_flag, and the structured ai_feedback"
    per the ask's own literal wording. Owner-exclusive (see the router's
    `current_user.id == video.student_id` check).

    All three added fields stay nullable/optional even though this
    endpoint is only ever reached for a row that's at least been
    dispatched to the pipeline -- a video still sitting in `PENDING_AI`
    (or one that ended in `FAILED` before Gemini's evaluation step ever
    ran) genuinely has no score/flag/feedback yet, and the owner checking
    in on an in-progress upload should see nulls, not a 404 or a
    fabricated placeholder.
    """

    ai_score: int | None = Field(default=None, ge=0, le=100)
    ai_safety_flag: bool
    ai_feedback: AIFeedback | None = None


class StudentVideoMyUploadOut(BaseModel):
    """`GET /student/videos/my-uploads` response item -- Real-time
    Student UGC Video Upload Tracker & Feedback Dashboard step.

    Field list matches this step's own ask (`id`, `title`, `status`,
    `created_at`, `ai_score`, `ai_feedback`), plus ONE disclosed
    necessary addition: `hls_master_url` (renamed from `hls_master_cid`,
    Student Video Pinata -> Cloudflare R2 Migration step). It isn't in
    the ask's literal field list, but the same ask's own frontend spec
    requires the APPROVED card's "View on Profile" button to open the
    video in `IPFSVideoPlayer` -- which needs a playable URL to render
    anything at all. Omitting it would make an explicitly-requested UI
    feature unbuildable from this endpoint's response alone.

    `status` is typed as the real `StudentVideoStatus` enum (not a bare
    `str`) -- direct response to this step's own verification
    requirement ("ensure any database states map cleanly to our existing
    StudentVideoStatus enum"). Pydantic v2 validates a plain string
    column value against a `(str, Enum)` member automatically, so this
    also acts as a runtime assertion that `StudentVideo.status` never
    contains a value outside the four defined states.

    Unlike `StudentVideoPrivateFeedbackOut`, `ai_safety_flag` is NOT
    included here -- not named in this step's own field list, and this
    dashboard's REJECTED card only needs `ai_feedback` (whose `summary`/
    `growth_points` already explain the outcome to the student) to
    satisfy "Read AI Coaching Review," not the raw boolean flag itself.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    status: StudentVideoStatus
    created_at: datetime
    hls_master_url: str | None
    # Hybrid Video Thumbnail System addition -- disclosed necessary
    # extension to this endpoint's response, beyond the Upload Tracker
    # step's own original field list: without it, the tracker
    # dashboard's `APPROVED` card would have no thumbnail image to show
    # at all, only the "View on Profile" button. Same "`None` until
    # resolved" posture as `StudentVideoOut.thumbnail_url` above.
    thumbnail_url: str | None
    ai_score: int | None = Field(default=None, ge=0, le=100)
    ai_feedback: AIFeedback | None = None


# =============================================================================
# Admin Override Dashboard & AI Decision Correction System
# =============================================================================


class AdminVideoReviewOut(BaseModel):
    """`GET /admin/videos/review-queue` response item.

    DISCLOSED, NOT `from_attributes=True` -- deliberately built by hand
    in `app/api/v1/endpoints/admin_moderation.py`'s `_build_review_item`
    helper rather than `AdminVideoReviewOut.model_validate(video)`. The
    reason is `ai_feedback`/`pipeline_error` below: `StudentVideo.ai_feedback`
    (a single JSONB column) holds TWO structurally different shapes
    depending on `status` -- for a `REJECTED` video it's Gemini's real
    structured evaluation (`{summary, strengths, growth_points, tags,
    transcode_metadata}`, see `app/tasks/student_video.py`'s success
    path); for a `FAILED` video it's an unrelated `{"error": "<exception
    message>"}` dict written by that same task's OWN failure path --
    never validated against `AIFeedback` at write time, because it isn't
    Gemini feedback at all, it's a raw pipeline-exception message.
    Naively trying to parse EVERY row's `ai_feedback` column as one
    `AIFeedback` object would raise a `ValidationError` on every single
    `FAILED` row in the review queue (missing `summary`/`strengths`/
    `growth_points`/`tags` keys) -- so this schema splits the two
    meanings into two separate, both-optional fields instead, and the
    helper function decides which one applies per row based on `status`.

    `student_name` is NOT a column on `StudentVideo` at all -- resolved
    via a `LEFT OUTER JOIN` against `users.full_name` in the router's own
    query (per this step's own explicit "Student Name" column
    requirement), `None` if the uploading user has no `full_name` set
    (a real, reachable case -- see `User.full_name`'s own nullable
    column) or (defensively) if the user row itself is somehow gone.
    """

    id: UUID
    title: str
    description: str | None
    student_id: UUID
    student_name: str | None
    status: StudentVideoStatus
    created_at: datetime
    hls_master_url: str | None
    thumbnail_url: str | None
    ai_score: int | None = Field(default=None, ge=0, le=100)
    # Populated only for a REJECTED video with real, schema-valid Gemini
    # output -- see class docstring.
    ai_feedback: AIFeedback | None = None
    # Populated only for a FAILED video (a pipeline exception message,
    # extracted from the same underlying `ai_feedback` JSONB column's
    # `"error"` key) -- see class docstring. Disclosed necessary
    # addition: without this, a `FAILED` row's "reason" would be
    # invisible to the admin reviewing it, even though this step's own
    # ask explicitly includes `FAILED` in the review queue's status
    # filter and asks to show "the Exact AI Feedback / Reason for
    # Rejection" for each flagged item.
    pipeline_error: str | None = None
    admin_override: bool
    admin_notes: str | None

    # --- Claim System (Prevent Duplicate Work on the Same Case) addition ---
    # This step's own Step 2.5 requirement: "show real claim state per
    # item... so the frontend can show this without a second round-trip
    # per item." All three reflect the EFFECTIVE claim state (an expired
    # claim already reads back as `None` here, same as never-claimed --
    # see `moderation_claim_service.get_effective_claim`), computed by
    # the router at query time, not read straight off the ORM row.
    claimed_by: UUID | None = None
    claimed_by_name: str | None = None
    claimed_at: datetime | None = None


class AdminOverrideRequest(BaseModel):
    """`POST /admin/videos/{video_id}/override` request body.

    `target_status` is validated to be EXACTLY `APPROVED` or `REJECTED`
    -- per this step's own literal "(must be APPROVED or REJECTED)"
    parenthetical. Rejected here at the Pydantic layer (a clean `422`
    with an explicit message) rather than left to fall through to
    whatever `StudentVideo.status` would otherwise silently accept --
    an admin "overriding" a video to `PENDING_AI` or `FAILED` isn't a
    moderation DECISION at all, it's just corrupting the pipeline's own
    state machine, and this step's ask is explicit that those two
    values are the only valid targets.
    """

    target_status: StudentVideoStatus
    notes: str | None = Field(default=None, max_length=2000)

    @field_validator("target_status")
    @classmethod
    def _validate_target_status(cls, value: StudentVideoStatus) -> StudentVideoStatus:
        if value not in (StudentVideoStatus.APPROVED, StudentVideoStatus.REJECTED):
            raise ValueError("target_status must be either APPROVED or REJECTED")
        return value


class AdminOverrideResponse(BaseModel):
    """`POST /admin/videos/{video_id}/override` response -- exactly the
    `{ success, video_id, new_status }` envelope this step's own ask
    specifies, word for word. Also reused, unmodified, as the response
    shape for `POST /admin/videos/{video_id}/reject-and-purge-media`
    below (Student Dashboard & Admin Control Panel step) -- the same
    three fields are exactly what that action's own frontend button
    needs back."""

    success: bool
    video_id: UUID
    new_status: StudentVideoStatus


# =============================================================================
# Student Dashboard & Admin Control Panel
# =============================================================================


class AdminRejectAndPurgeRequest(BaseModel):
    """`POST /admin/videos/{video_id}/reject-and-purge-media` request
    body -- this step's own "Reject & Delete" action. Deliberately NOT
    `AdminOverrideRequest` reused as-is: that schema's `target_status`
    field lets the CALLER choose between `APPROVED`/`REJECTED`, but this
    action's target is always `REJECTED` by definition (it's the
    dedicated "reject and purge storage" button, not a general-purpose
    status setter) -- a request body that still accepted a
    `target_status` field here would be misleading about what this
    endpoint actually does. `notes` is the only real input, same
    optional-reason shape as `AdminOverrideRequest.notes`.
    """

    notes: str | None = Field(default=None, max_length=2000)
