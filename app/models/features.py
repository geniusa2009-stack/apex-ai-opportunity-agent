"""Hyper-Personalized Platform step: "Advanced Integrated Schemas"
(`app/models/features.py`, per that step's own literal file placement).

Three models, all genuinely new (no pre-existing equivalent found anywhere
in this codebase -- confirmed via a full search of `app/models/` before
writing any of this, see task #437's research). Shared conventions/
judgment calls used across all three, each also noted on its own class:

- No `relationship()` ORM objects back to `User`/`Course`/`Lesson`/
  `StudentVideo` are added here, and none of THOSE models gain a new
  back-populated collection (e.g. no `User.certificates`). This mirrors
  this codebase's own existing precedent for structurally identical
  "join/record" tables that carry FKs but aren't primarily navigated FROM
  their parent (`UserEnrollment`, `LessonProgress` -- see
  `app/models/courses.py`, neither of which declares a `relationship()`
  either). Contrast with `UserBookmark`, which DOES get relationships on
  both sides because `User.bookmarks`/`Opportunity.bookmarks`/
  `Course.bookmarks` are actually read as eager-loaded collections
  elsewhere in this codebase already -- no such existing read pattern
  exists for certificates/study-aids/mentorships, so no relationship
  objects are speculatively added for this pass. A future endpoint that
  needs "all of a user's certificates" can query `select(Certificate)
  .where(Certificate.user_id == ...)` directly, the same way
  `course_service.py` already queries `UserEnrollment` directly.
- Every FK uses `ondelete="CASCADE"`, this codebase's established default
  posture for every FK in this project (see e.g. `UserEnrollment`'s own
  docstring on exactly this point).
- `id: UUID` primary keys throughout (not composite), since none of these
  three models was described by the ask using literal "PrimaryKey with
  ..." wording the way `UserEnrollment` explicitly was -- same "PrimaryKey
  is only taken literally when the ask says PrimaryKey" posture already
  established in `app/models/courses.py`'s own module docstring point 1.
"""

import datetime as dt
import uuid

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import MentorshipStatus


class Certificate(Base):
    """A completion certificate issued to a user for a course.

    Exactly the four fields the ask names: `user_id`, `course_id`,
    `issued_at`, `certificate_url`.

    `UniqueConstraint("user_id", "course_id")` -- disclosed necessary
    addition, not named by the ask directly: without it, nothing stops
    the (not-yet-built, see README) certificate-issuance code path from
    creating duplicate certificates for the same user finishing the same
    course twice (e.g. a retried request). One certificate per (user,
    course) pair is the obvious intended invariant -- same reasoning
    `LessonProgress`'s own `UniqueConstraint` docstring already gives for
    an identical shape of gap.

    `certificate_url` is nullable: this step's own ask is schema-only
    ("Certificate Model: user_id, course_id, issued_at, certificate_url")
    -- no certificate-PDF-generation pipeline is named or built in this
    pass (disclosed scope boundary, see README). A row can therefore
    exist with `certificate_url IS NULL` in between "issued" and "PDF
    actually rendered and uploaded," the same nullable-until-populated
    posture already used for `Course.thumbnail_cid`/`Lesson.thumbnail_cid`
    elsewhere in this codebase.
    """

    __tablename__ = "certificates"
    __table_args__ = (
        UniqueConstraint("user_id", "course_id", name="uq_certificate_user_course"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    course_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("courses.id", ondelete="CASCADE"), nullable=False, index=True
    )

    issued_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    certificate_url: Mapped[str | None] = mapped_column(String(2048), nullable=True)

    def __repr__(self) -> str:
        return f"<Certificate user_id={self.user_id} course_id={self.course_id}>"


class LessonStudyAid(Base):
    """AI-generated flashcards + summary for one piece of learning content
    (the ask's own "AI Flashcards & Summaries Model (Maturity 3)").

    DISCLOSED JUDGMENT CALL -- dual-target linking shape: the ask says
    this "Links to lessons/videos" without specifying whether that means
    a single polymorphic FK, two separate nullable FKs, or something else.
    Resolved with the EXACT SAME pattern just built for `UserBookmark`
    (`app/models/bookmark.py`) two models above -- two nullable FKs
    (`lesson_id` -> `lessons.id`, `student_video_id` -> `student_videos.id`)
    plus a `CheckConstraint` requiring exactly one set. Chosen over a
    single untyped polymorphic id column (like `VideoPlaybackLog.video_id`,
    `app/models/analytics.py`) specifically BECAUSE this table's rows are
    meant to be looked up FROM their parent content item ("give me this
    lesson's flashcards") via a real, indexed, integrity-checked FK --
    `VideoPlaybackLog`'s own docstring explains why IT couldn't use a real
    FK (two disjoint ID spaces with no natural single-column target,
    written purely as an outbound analytics log); that constraint doesn't
    apply here.

    "Videos" is read as `StudentVideo` (this codebase's only other video
    content type with a transcript already available to summarize --
    `StudentVideo.transcript_text`, added in the Student Video Profiles
    step) rather than `Lesson`'s `video_url`/`hls_master_cid` fields
    (already covered by `lesson_id` itself, since a `Lesson` IS the
    content unit regardless of whether it happens to embed a video).

    `summary_text`/`flashcards` are exactly the two fields the ask names,
    with its own literal types (`Text`, `JSONB`). Both nullable: per the
    ask's own "generated asynchronously" wording, a row's existence
    doesn't guarantee generation has finished yet -- same "nullable until
    a background job populates it" posture as `StudentVideo.ai_feedback`
    before its own grading pipeline completes. No explicit generation-
    status column is added (the ask names exactly two content fields, not
    a status machine); a future pass can treat "both fields NULL" as "not
    generated yet" without needing one, the same inference already used
    for `Lesson.video_processing_status IS NULL` meaning "never
    processed."

    `unique=True` (column-level, not a table `UniqueConstraint`) on both
    FK columns: at most one study aid per lesson and at most one per
    video. Column-level `unique=True` on a NULLABLE column is safe here --
    Postgres (and SQLite) both treat every NULL as distinct from every
    other NULL for uniqueness purposes, so the many rows with
    `lesson_id IS NULL` (video-linked rows) never collide with each other
    under `student_video_id`'s own separate uniqueness, and vice versa --
    same reasoning already disclosed on `UserBookmark`'s two parallel
    `UniqueConstraint`s.
    """

    __tablename__ = "lesson_study_aids"
    __table_args__ = (
        CheckConstraint(
            "(lesson_id IS NOT NULL AND student_video_id IS NULL) "
            "OR (lesson_id IS NULL AND student_video_id IS NOT NULL)",
            name="ck_study_aid_exactly_one_target",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    lesson_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("lessons.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )
    student_video_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("student_videos.id", ondelete="CASCADE"),
        nullable=True,
        unique=True,
        index=True,
    )

    summary_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    flashcards: Mapped[list[dict] | None] = mapped_column(JSONB, nullable=True)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<LessonStudyAid lesson_id={self.lesson_id} "
            f"student_video_id={self.student_video_id}>"
        )


class MentorshipRelation(Base):
    """A mentor <-> mentee relationship between two `User`s (the ask's own
    "Mentorship Relation Model (Maturity 7)").

    `status` -- plain `String` column backed by the `MentorshipStatus`
    Python enum (`app/models/enums.py`), NOT a native Postgres ENUM type.
    Follows this codebase's own established convention for lifecycle-
    status columns (`TrackerStatus`, `FocusSessionStatus`,
    `StudentVideoStatus`, `VideoProcessingStatus` -- see each one's own
    docstring in `enums.py`): a native Postgres ENUM is only used when an
    ask explicitly asks for one, or for small/stable CATEGORY enums like
    `OpportunityType`/`AcademicLevel`. This ask writes "status (PENDING,
    ACTIVE, COMPLETED)" the same bare way `FocusSessionStatus`'s ask did,
    not an explicit "native Postgres ENUM" instruction -- so it gets the
    same plain-String-plus-Pydantic-validation treatment for cheap,
    migration-free extensibility if a future state (e.g. `REJECTED`,
    genuinely absent from this ask's own three-value list) is ever needed.

    Two disclosed, necessary additions beyond the ask's bare
    "mentor_id/mentee_id -> User, status" spec:

    1. `CheckConstraint` `ck_mentorship_not_self` -- `mentor_id !=
       mentee_id`. A user mentoring themselves is a real, avoidable data
       integrity problem the ask doesn't need to spell out for it to be
       obviously wrong, the same class of "not asked for directly, but
       clearly required" call already made for `UserEnrollment`'s cascade
       deletes.
    2. `UniqueConstraint` on `(mentor_id, mentee_id)` -- at most one
       relationship row per ordered pair. DISCLOSED SCOPE BOUNDARY: this
       means a COMPLETED (or otherwise closed-out) mentorship between the
       same two users can't be "re-requested" as a brand new row later --
       any future re-request flow would need to reuse/reset this same
       row's `status` rather than insert a second one. The ask names no
       re-request/history requirement, so this simpler single-row-per-pair
       model was chosen over a more complex append-only history table.
    3. `responded_at: datetime | None` -- disclosed necessary addition,
       not named by the ask: `requested_at` alone can't distinguish "still
       pending" from "the mentor acted on this," and the `/mentorship`
       router (this step's own ask) needs a real timestamp to show when an
       approval/rejection happened. Nullable, set only when
       `status` transitions away from `PENDING`.
    """

    __tablename__ = "mentorship_relations"
    __table_args__ = (
        UniqueConstraint("mentor_id", "mentee_id", name="uq_mentorship_pair"),
        CheckConstraint("mentor_id != mentee_id", name="ck_mentorship_not_self"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    mentor_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    mentee_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    status: Mapped[MentorshipStatus] = mapped_column(
        String(20), default=MentorshipStatus.PENDING, nullable=False
    )

    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<MentorshipRelation mentor_id={self.mentor_id} "
            f"mentee_id={self.mentee_id} status={self.status}>"
        )
