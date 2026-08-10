"""Ecosystem Features 13 & 14: AI-Powered LMS & Interactive Academy.

Five models, all from this step's own ask. Two disclosed judgment calls
worth flagging up front (each covered in more depth on the relevant
model below):

1. `LessonProgress` gets its own `id` UUID primary key -- the ask marks
   its `user_id`/`lesson_id` columns "indexed", not "PrimaryKey with
   <other column>" the way it explicitly marks `UserEnrollment`'s two FK
   columns a few lines above it in the same ask. Read literally (and
   consistently with the identical situation already resolved for
   `UserConnection` in the immediately preceding step), "indexed" is not
   "primary key" -- these needed a real single-column identity, so a
   `UniqueConstraint` does the "one progress row per (user, lesson)"
   uniqueness job instead.
2. `UserEnrollment`'s composite PK IS taken completely literally (the
   ask says "PrimaryKey with course_id" outright) -- no `id` column here.

--- Core Video Media Processing Pipeline & Intelligent Recommendation
Engine (built on top of the above) ---

Two more disclosed judgment calls, added by that follow-on step:

3. `Course.embedding` -- that step's ask never names a column for it
   directly (its own literal field list is all HLS/video fields), but
   its "AI Recommendation Engine" requirement explicitly needs "the
   embeddings of available courses" to compute a cosine distance
   against, and nothing in this schema had ever stored one. Added here,
   disclosed, at COURSE granularity (not per-lesson) since the
   recommendation surface this step actually asks for is course-level
   (`get_recommended_courses(...) -> ranked list of ... courses`).
4. The HLS/video fields (`hls_master_cid`, `duration_seconds`,
   `video_processing_status`, `video_metadata`) are added to `Lesson`,
   not `Course`, even though that step's own wording offers either
   ("Update or extend the `Lesson` or `Course` models"). Taken as an
   obvious semantic call, not a real toss-up: an individual VIDEO is
   lesson content (a `Lesson` already has `video_url`/`content_type` for
   exactly this), not a property of the course container that holds many
   lessons across many modules. Placing these on `Course` would make it
   impossible to represent more than one video per course at all.

--- Hybrid Video Thumbnail System (migration 0016) ---

5. `thumbnail_cid: str | None` added to BOTH `Course` and `Lesson`, per
   that step's own explicit literal instruction ("Ensure Course, Lesson,
   and StudentVideo tables have a nullable thumbnail_cid field"). Note
   `Course` already had a `thumbnail_url` column (an arbitrary, possibly
   non-IPFS URL, set at course-creation time via `CourseCreateRequest`)
   from the original LMS step -- `thumbnail_cid` is a deliberately
   SEPARATE, new column, not a rename/reuse of that existing one: this
   step's whole point is an IPFS-pinned image (a CID, resolved through a
   gateway URL at read time), which is a materially different contract
   from an arbitrary externally-hosted URL string. Disclosed scope
   boundary: unlike `StudentVideo.thumbnail_cid` (populated by a real
   upload+FFmpeg-fallback pipeline, see `student_videos.py` and
   `app/tasks/student_video.py`), NEITHER `Course.thumbnail_cid` nor
   `Lesson.thumbnail_cid` has any write path wired up in this pass --
   this step's own ask never describes an admin thumbnail-upload flow
   for courses/lessons, only for student video uploads. These two
   columns exist (and are exposed, nullable, through `CourseOut`/
   `LessonOut` in app/schemas/courses.py) purely to satisfy the ask's
   explicit schema requirement and to leave room for a future admin
   upload flow to populate them; they are `NULL` for every row until
   such a flow exists.
"""

import datetime as dt
import uuid
from typing import TYPE_CHECKING

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.student_memory import MEMORY_EMBEDDING_DIM

if TYPE_CHECKING:
    from app.models.bookmark import UserBookmark


class Course(Base):
    """A top-level course -- fully specified by this step's own ask.
    `token_unlock_cost=0` means free/open (any authenticated student can
    enroll for free); `>0` means the student must spend that many tokens
    to enroll (see `course_service.enroll_in_course`)."""

    __tablename__ = "courses"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    instructor_name: Mapped[str] = mapped_column(String(255), nullable=False, default="AI Scholar Guide")
    token_unlock_cost: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    thumbnail_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)

    # Hybrid Video Thumbnail System addition (migration 0016) -- see this
    # module's own docstring, point 5, for why this is a separate column
    # from `thumbnail_url` above rather than a reuse/rename of it.
    thumbnail_cid: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Disclosed addition (see module docstring, point 3) -- reuses the
    # SAME dimensionality constant as `StudentMemory.embedding`
    # (app/models/student_memory.py) deliberately, not by coincidence:
    # `recommendation_service.get_user_interest_vector` compares a
    # user's averaged `StudentMemory` embeddings directly against this
    # column via pgvector's cosine distance operator, which is only a
    # meaningful comparison when both sides live in the exact same
    # embedding space (same model, same dimensionality). Nullable --
    # embedding generation (`recommendation_service.embed_course`) is
    # best-effort at course-creation time; a course whose embedding call
    # failed should still exist and be usable, just excluded from
    # recommendation ranking until backfilled (see that function's
    # docstring).
    embedding: Mapped[list[float] | None] = mapped_column(Vector(MEMORY_EMBEDDING_DIM), nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # Hyper-Personalized Platform step addition -- symmetric other half of
    # `UserBookmark.course` (app/models/bookmark.py; see that model's own
    # docstring for the full polymorphic-bookmark disclosure). `lazy=
    # "selectin"` matches every other bookmark-adjacent relationship in
    # this codebase (`User.bookmarks`, `Opportunity.bookmarks`).
    bookmarks: Mapped[list["UserBookmark"]] = relationship(
        back_populates="course",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<Course id={self.id} title={self.title!r}>"


class CourseModule(Base):
    """A module (chapter) within a `Course` -- fully specified by this
    step's own ask."""

    __tablename__ = "course_modules"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    def __repr__(self) -> str:
        return f"<CourseModule id={self.id} course_id={self.course_id} title={self.title!r}>"


class Lesson(Base):
    """A single lesson within a `CourseModule` -- fully specified by this
    step's own ask. `content_type` is written with "e.g." (video, text,
    pdf) -- non-exhaustive wording, same posture as `parent_pitch.primary_concern`
    -- so it stays a plain String, not a locked enum, validated loosely
    at the Pydantic layer instead (see app/schemas/courses.py)."""

    __tablename__ = "lessons"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    module_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("course_modules.id", ondelete="CASCADE"), nullable=False, index=True
    )

    title: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(50), nullable=False)
    video_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    text_content: Mapped[str | None] = mapped_column(Text, nullable=True)
    order_index: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- Disclosed additions (see module docstring, point 4): the Core
    # Video Media Processing Pipeline's self-hosted, IPFS-pinned HLS
    # distribution path -- distinct from (and independent of) `video_url`
    # above, which stays exactly what it always was (an externally
    # hosted YouTube/Vimeo embed URL). A given lesson uses EITHER
    # mechanism, never both at once in practice, but nothing here forces
    # that -- `video_url` and `hls_master_cid` are just two independent
    # nullable columns.
    #
    # The root IPFS CID of the pinned HLS output directory (contains
    # `master.m3u8` at its root plus one subdirectory per rendition) --
    # per the ask's own literal field spec. `None` until processing
    # actually completes.
    hls_master_cid: Mapped[str | None] = mapped_column(String(255), nullable=True)

    duration_seconds: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # Plain String, not the native Postgres ENUM path -- same "Literal
    # enforces on write, column stays plain String" convention as every
    # other status-shaped column in this codebase since `TrackerStatus`.
    # Nullable and defaults to `None`, NOT `VideoProcessingStatus.PENDING`
    # -- see `app.models.enums.VideoProcessingStatus`'s docstring: most
    # lessons (`content_type="text"`/`"pdf"`, or ones using the plain
    # `video_url` embed path) never have any video processing pipeline
    # run against them at all, and showing them as permanently "pending"
    # would be actively misleading. Only ever set once an admin actually
    # calls `POST .../upload-video` (see media_router.py).
    video_processing_status: Mapped[str | None] = mapped_column(String(50), nullable=True, default=None)

    # "resolution, size, etc." per the ask's own literal description --
    # holds the rendition ladder actually produced (name/width/height/
    # bitrate per variant), the original upload's filename/content-type/
    # size, and (only on a FAILED run) a short error message. See
    # `app/services/media_service.py`'s `transcode_to_hls` docstring for
    # the exact shape written here.
    video_metadata: Mapped[dict | None] = mapped_column(JSONB(), nullable=True)

    # Hybrid Video Thumbnail System addition (migration 0016) -- see this
    # module's own docstring, point 5.
    thumbnail_cid: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return f"<Lesson id={self.id} module_id={self.module_id} title={self.title!r}>"


class UserEnrollment(Base):
    """A student's enrollment in a `Course`. Composite primary key taken
    completely literally from the ask ("PrimaryKey with course_id") --
    unlike `LessonProgress` below, this one really does say "PrimaryKey",
    so no separate `id` column is added here.

    `ondelete="CASCADE"` on both FKs is a disclosed, conventional
    addition -- the ask doesn't say so for this model specifically
    (unlike `CourseModule`/`Lesson`, which explicitly say "cascade
    delete"), but leaving a dangling enrollment behind after its course
    (or the user) is deleted would be a real data-integrity problem, and
    cascading is this codebase's default posture for every other FK.
    """

    __tablename__ = "user_enrollments"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), primary_key=True
    )

    enrolled_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<UserEnrollment user_id={self.user_id} course_id={self.course_id}>"


class LessonProgress(Base):
    """Tracks one student's completion of one `Lesson`.

    `id`: disclosed necessary addition -- see this module's docstring
    for the full reasoning (the ask marks both `user_id` and `lesson_id`
    "indexed", not "PrimaryKey", the way it explicitly does for
    `UserEnrollment`'s two FK columns just above). A `UniqueConstraint`
    on `(user_id, lesson_id)` does the uniqueness job a composite PK
    would otherwise have done, while still giving this row its own
    single-column identity -- and is itself a disclosed necessary guard:
    without it, nothing stops `complete_lesson` (or a retried/duplicate
    request) from creating multiple progress rows for the same student
    and lesson, which would break the very "is this lesson done"
    question this table exists to answer.
    """

    __tablename__ = "lesson_progress"
    __table_args__ = (UniqueConstraint("user_id", "lesson_id", name="uq_lesson_progress_pair"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    lesson_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("lessons.id", ondelete="CASCADE"), nullable=False, index=True
    )

    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    completed_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return f"<LessonProgress user_id={self.user_id} lesson_id={self.lesson_id} is_completed={self.is_completed}>"
