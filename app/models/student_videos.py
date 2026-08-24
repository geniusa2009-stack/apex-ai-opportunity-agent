"""Student Video Profiles & AI-Moderated Upload System.

One model, `StudentVideo`, fully specified by this step's own ask. Its
lifecycle enum, `StudentVideoStatus`, is defined in `app/models/enums.py`
instead of directly in this file -- see that enum's own docstring for the
disclosed reasoning (this codebase centralizes every status-lifecycle
enum there; this is imported from there below).

Two disclosed conventional additions, consistent with every other model
in this codebase, neither literally requested by this step's ask:

1. `ondelete="CASCADE"` on the `student_id` foreign key. Not stated for
   this model specifically, but this codebase's uniform default posture
   for every FK to `users.id` (see e.g. `UserEnrollment`/`LessonProgress`
   in app/models/courses.py) -- a video record surviving its own owner's
   deleted account would be a real data-integrity/orphan-content problem,
   and there's no reason this table should be the one exception.
2. A composite index on `(student_id, status)` and a separate index on
   `created_at` are both explicitly asked for here (unlike most prior
   steps, where such indexes were disclosed additions) -- implemented
   via `__table_args__` below exactly as specified, since a single
   `index=True` on each individual column would give two separate
   single-column indexes, not the one COMPOSITE `(student_id, status)`
   index this ask explicitly names.

Automated WebVTT Captions Integration step (migration 0015) added three
more columns, disclosed at length there and reiterated briefly here: this
model previously had NOWHERE to put the Whisper transcript at all -- the
original ask's own component 3/4 pipeline only ever held the transcript
in a local Python variable, passed it to Gemini for evaluation, and then
let it fall out of scope. Building real, time-synced captions requires
persisting it. `transcript_text` (flat string, also directly reusable by
a future "read the transcript" UI) and `transcript_segments` (JSONB list
of `{start, end, text}` -- Whisper's own per-segment timestamps, once the
transcription call itself was switched from `response_format="text"` to
`"verbose_json"`; see `student_video_service.py`) are both nullable,
consistent with every other post-pipeline field here. `duration_seconds`
is populated from that SAME verbose_json response's own `duration` field
(the audio track's duration, which equals the source video's duration)
rather than a second `ffprobe` subprocess call against the raw video --
the data is already being fetched at that pipeline step, so a second
probe of the same underlying duration would be pure waste.

Hybrid Video Thumbnail System step (migration 0016) added one more
column, `thumbnail_cid`, disclosed at length in `app/tasks/student_video.py`
and `app/services/student_video_service.py`: nullable, populated by
EITHER a viewer-supplied custom image (uploaded immediately alongside the
video, per the router) OR -- if none was supplied -- an FFmpeg-extracted
first-second video frame, pinned to IPFS by the Celery pipeline itself.
Unlike `hls_master_cid`/`ai_score`/etc., this column is deliberately
populated as a BEST-EFFORT step that can never fail the surrounding
pipeline run -- see `resolve_thumbnail_cid`'s own docstring for why a
thumbnail failure (e.g. a corrupt frame, a transient Pinata error) must
not turn an otherwise-successful transcode+moderation run into a FAILED
video. `None` for any row whose pipeline predates this migration, AND
for any row where thumbnail resolution itself simply never succeeded.

Admin Override Dashboard & AI Decision Correction System step (migration
0017) added two more columns, `admin_override`/`admin_notes`, fully
specified by this step's own ask. `admin_override` defaults to `False`
and flips to `True` the moment ANY admin calls `POST /admin/videos/
{video_id}/override` against this row -- a simple boolean flag, not a
full audit log: it answers "was this video's current status ever
manually corrected by a human," not "by whom, or how many times." See
`app/api/v1/endpoints/admin_moderation.py`'s own module docstring for
the disclosed scope boundary this implies (no `overridden_by_admin_id`
FK, no history of prior overrides -- only this step's own literal two
fields). `admin_notes` is a single nullable free-text field holding the
admin's stated reason for the MOST RECENT override -- overwritten (not
appended to) by a second override call, consistent with it being one
scalar column rather than a list/table of notes.

Student Video Pinata -> Cloudflare R2 Migration step (migration 0018)
RENAMED two existing columns: `hls_master_cid` -> `hls_master_url`, and
`thumbnail_cid` -> `thumbnail_url`. Disclosed, necessary rename, not a
cosmetic one: both columns used to hold a genuine IPFS content
identifier (a CID -- content-addressed, meaningless without a gateway to
resolve it against, see `lib/ipfsGateways.ts`'s `IPFS_GATEWAYS`
fallback list on the frontend). This pipeline's HLS output and
thumbnails now live on Cloudflare R2 instead (see `app/services/
r2_service.py` and `app/core/config.py`'s R2 settings block) -- an R2
object has exactly one real, directly-playable public URL, not a
content-addressed identifier requiring a second gateway-resolution step.
Continuing to call these columns "`*_cid`" once they held a URL instead
of a CID would be actively misleading to every future reader of this
model, so they were renamed alongside the storage-backend swap rather
than silently repurposed under their old names. `Course.hls_master_cid`/
`Lesson.thumbnail_cid` (app/models/courses.py) are UNCHANGED and still
hold real IPFS CIDs -- this migration's own disclosed scope is the
Student Video pipeline only; the Core Video Media Processing Pipeline
(admin-authored Course/Lesson videos) was explicitly not migrated, see
`app/core/config.py`'s R2 settings block for the product reasoning.
Both renamed columns keep the exact same nullability/semantics as
before (`None` until pipeline resolution succeeds; thumbnail resolution
remains best-effort per `resolve_thumbnail_url`'s own docstring in
`student_video_service.py`) -- only the column NAME and the underlying
storage backend changed, not the lifecycle.
"""

import datetime as dt
import uuid

from pgvector.sqlalchemy import Vector
from sqlalchemy import Boolean, DateTime, ForeignKey, Index, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import StudentVideoStatus

# OpenAI's text-embedding-3-small, 1536-dim -- same model AND dimension
# as `Opportunity.embedding` (app/models/opportunity.py, see
# `app/services/ai.py`'s `EMBEDDING_DIM` constant), deliberately reused
# rather than introducing a third embedding space in this codebase (the
# other being Gemini's 768-dim `text-embedding-004`, used by
# `StudentMemory.embedding`/`Course.embedding`). This ask's own step 5
# explicitly calls for `text-embedding-3-small`, which is exactly what
# `ai_gateway.generate_embedding` (app/services/ai.py) already wraps --
# reused directly rather than adding a parallel OpenAI embedding client.
_SUMMARY_EMBEDDING_DIM = 1536


class StudentVideo(Base):
    """A student-uploaded presentation/project video and its
    AI-moderated review outcome. Fully specified by this step's own ask.

    `hls_master_url` (renamed from `hls_master_cid`, see this class's own
    column comment and the module docstring above) is nullable "until
    completed" per the ask's own wording -- `None` for the entire
    `PENDING_AI` window (and forever, if the pipeline ends in `FAILED`),
    populated only once the HLS-transcode-and-upload sub-step of
    `process_student_video_pipeline_task` actually succeeds.

    `ai_score`/`ai_safety_flag`/`ai_feedback`/`summary_embedding` are all
    nullable for the same reason -- none of them exist until Gemini's
    evaluation step actually runs and returns.
    """

    __tablename__ = "student_videos"
    __table_args__ = (
        Index("ix_student_videos_student_id_status", "student_id", "status"),
        Index("ix_student_videos_created_at", "created_at"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    student_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)

    # RENAMED from `hls_master_cid` (migration 0018, Student Video Pinata
    # -> Cloudflare R2 Migration) -- see module docstring. Now holds the
    # full, directly-playable public R2 URL to this video's
    # `master.m3u8` (e.g. `https://pub-xxxx.r2.dev/student-videos/{id}/
    # hls/master.m3u8`), produced by `media_service.transcode_to_hls` +
    # `r2_service.upload_hls_directory_to_r2`. No longer shares a
    # contract with `Lesson.hls_master_cid` (app/models/courses.py, which
    # remains a real IPFS CID on Pinata) -- that field is a DIFFERENT
    # pipeline, unaffected by this migration.
    hls_master_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # Plain String, not a native Postgres ENUM type -- see
    # `StudentVideoStatus`'s own docstring for why. `index=True` here is
    # additionally covered by the composite `(student_id, status)` index
    # above for the "this student's videos by status" query shape, but
    # kept as its own single-column index too since a status-only filter
    # (independent of any one student -- e.g. an admin moderation queue
    # over all `PENDING_AI` rows) is also a realistic access pattern this
    # ask's own wording ("Indexed") supports.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=StudentVideoStatus.PENDING_AI.value, index=True
    )

    # "Integer (0-100, nullable)" per the ask's own literal spec -- no DB
    # CHECK constraint enforcing that range, consistent with this
    # codebase's established posture (e.g. `QuizQuestion.correct_answer_index`)
    # of enforcing numeric ranges at the Pydantic layer, not the DB
    # schema layer.
    ai_score: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # True once Gemini's `safety_approved` comes back `false` -- i.e. this
    # flag and `status == REJECTED` are set together, from the same
    # evaluation call. Kept as its own boolean column (rather than only
    # inferring it from `status`) because the ask names it as a distinct
    # field, and because a moderation dashboard querying "show me every
    # flagged video regardless of overall status" is a real, different
    # access pattern than "show me every REJECTED video."
    ai_safety_flag: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # "schema validated: summary, strengths, growth_points, tags" per the
    # ask -- validated by `schemas.student_videos.AIFeedback` in the
    # service layer BEFORE being written here, not by a DB-level JSON
    # Schema constraint (Postgres has no built-in JSONB schema
    # validation; enforcing this at the Pydantic layer is this
    # codebase's uniform posture for every other structured-JSON column,
    # e.g. `Lesson.video_metadata`).
    ai_feedback: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)

    summary_embedding: Mapped[list[float] | None] = mapped_column(
        Vector(_SUMMARY_EMBEDDING_DIM), nullable=True
    )

    # --- Automated WebVTT Captions Integration additions (migration 0015) ---
    # See module docstring for why these three didn't exist before this
    # step. All nullable: `None` until the pipeline's transcription
    # sub-step actually runs and returns (same "doesn't exist until that
    # stage succeeds" posture as `ai_score`/`ai_feedback` above), and
    # permanently `None` for any video whose pipeline run predates this
    # migration.
    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)
    transcript_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    # List of `{"start": float, "end": float, "text": str}` objects, one
    # per Whisper-reported speech segment -- NOT schema-validated by a
    # Pydantic model the way `ai_feedback` is, since this is internal
    # pipeline data never taken as external input (only ever written by
    # the Celery task itself from a trusted Whisper API response), unlike
    # `ai_feedback`, which is validated because a malformed/unexpected
    # Gemini response is a real, reachable failure mode worth catching
    # structurally.
    transcript_segments: Mapped[list | None] = mapped_column(JSONB(), nullable=True)

    # --- Hybrid Video Thumbnail System addition (migration 0016) --- see
    # module docstring above. RENAMED from `thumbnail_cid` to
    # `thumbnail_url` (migration 0018, Student Video Pinata -> Cloudflare
    # R2 Migration) -- now holds the full public R2 URL to the resolved
    # thumbnail image (custom-uploaded or FFmpeg-extracted, same
    # best-effort semantics as before), not an IPFS CID. `String(500)`,
    # widened from the CID column's original `String(255)` -- a full URL
    # (scheme + host + bucket path) has real headroom to exceed a bare
    # 46-character IPFS CIDv1 string, so the old length was tightened for
    # a value type it no longer holds.
    thumbnail_url: Mapped[str | None] = mapped_column(String(500), nullable=True)

    # --- Admin Override Dashboard & AI Decision Correction System
    # additions (migration 0017) --- see module docstring above.
    admin_override: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    admin_notes: Mapped[str | None] = mapped_column(String, nullable=True)

    # --- Claim System (Prevent Duplicate Work on the Same Case) addition
    # (migration 0039) --- see `app/api/v1/endpoints/admin_claims.py`'s
    # own module docstring for the full "what is a case" reasoning: the
    # review queue's only real item kind today is a `StudentVideo` row,
    # so a claim is two fields added directly to it rather than a
    # separate polymorphic table -- the review-queue listing already
    # `SELECT`s this row, so surfacing claim state per item costs zero
    # extra queries, satisfying that step's own "no second round-trip
    # per item" requirement for free.
    #
    # `claimed_by` uses `ondelete="SET NULL"` (not `CASCADE`) -- same
    # posture as `ModerationLogEntry.actor_id`: if a moderator's account
    # is deleted mid-claim, the CLAIM shouldn't survive as a permanent
    # dangling reference, but the VIDEO row itself must not be touched.
    # `claimed_at` deliberately stays non-`None` after a `SET NULL`
    # (nothing re-nulls it) -- harmless, because every real read site
    # (`moderation_claim_service.get_effective_claim`) treats
    # `claimed_by IS NULL` as sufficient on its own to mean "unclaimed,"
    # never reading `claimed_at` in isolation.
    claimed_by: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    claimed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<StudentVideo id={self.id} student_id={self.student_id} status={self.status!r}>"
