"""Video Analytics, Watch Time Tracking & Engagement Engine.

One model, `VideoPlaybackLog`, fully specified by this milestone's own
ask: tracks per-(user, video) engagement -- last playback position (so a
student can resume where they left off), cumulative watch time
(accumulating monotonically across many short heartbeat pulses, never
reset), and a completion flag (95%-watched-or-ended).

--- Disclosed judgment calls -------------------------------------------

1. **Polymorphic `video_id` + `video_type`, no foreign key.** This
   milestone's own ask is explicit: `video_id: UUID` ("Can map to
   StudentVideo or Lesson") and `video_type: String` ('UGC' | 'COURSE').
   That's a single id column meant to point into EITHER of two
   completely separate tables (`student_videos.id` or `lessons.id`)
   depending on the sibling `video_type` column's value -- Postgres has
   no native mechanism for a foreign key that conditionally targets one
   of two different tables, so `video_id` is a plain, unconstrained
   `UUID` column here, exactly as literally specified. The trade-off,
   disclosed rather than hidden: the database itself cannot guarantee a
   given `video_id` actually still exists in the table its `video_type`
   implies -- referential integrity for this relationship is enforced at
   the APPLICATION layer instead (`app/api/v1/endpoints/analytics.py`'s
   heartbeat handler looks the video up in the correct table, keyed off
   `video_type`, and 404s if it's missing, before ever writing a
   playback log row against it). This is the same class of trade-off
   this codebase already accepts elsewhere for a genuinely polymorphic
   relationship (e.g. `AdminVideoReviewOut.ai_feedback`/`pipeline_error`
   splitting one JSONB column's two possible shapes by a sibling
   `status` column) -- reached for here because the ask's own schema is
   unambiguous about wanting ONE `video_id` column, not two separate
   nullable FK columns (`student_video_id`/`lesson_id`) with a CHECK
   constraint enforcing exactly one is set.
2. **`user_id` is nullable, per the ask's own literal wording** ("nullable
   for public unregistered views if needed, but required for students").
   In practice, THIS milestone's only write path (`POST /analytics/
   heartbeat`, `CurrentUser`-gated) never writes a `NULL` user_id -- the
   endpoint requires authentication outright, so every real row
   currently written has a real `user_id`. The column stays nullable
   purely to leave room for a genuinely anonymous/public tracking
   endpoint in a future step, without a second migration to loosen a
   `NOT NULL` constraint later. `ondelete="CASCADE"` on the FK (when
   non-null) matches this codebase's uniform default posture for every
   other FK to `users.id`.
3. **`UniqueConstraint(user_id, video_id, video_type)`**, disclosed
   necessary addition -- not literally named by the ask, but required
   for the ask's own "atomic upsert... for the user + video combination"
   business logic to mean anything at all: without a real uniqueness
   guarantee at the database level, nothing stops two concurrent
   heartbeat requests for the same student watching the same video from
   racing an INSERT and creating two separate rows, silently splitting
   that student's watch-time/position data across them. The router's
   `SELECT ... FOR UPDATE` + `IntegrityError`-on-losing-INSERT handling
   (same established pattern as `coach_service.get_or_create_accountability`/
   `course_service.enroll_in_course`) depends directly on this
   constraint existing. NULL `user_id` values are NOT covered by this
   guarantee (Postgres unique constraints treat `NULL <> NULL`) -- a real
   consideration only once an anonymous-tracking write path is ever
   added; the sole current write path (`CurrentUser`-gated) never hits
   that edge.
4. **`last_position_seconds`/`cumulative_watch_time_seconds` are `Float`,
   not `Integer`**, per the ask's own literal `Float` type. A native
   HTML5 `<video>` element's `currentTime` is a genuine sub-second
   float, and truncating it to whole seconds would make the "resume
   exactly where they left off" feature noticeably imprecise (visibly
   snapping backward by up to a second on every resume).
5. **`updated_at` uses `onupdate=func.now()`**, a disclosed necessary
   addition -- the ask names the column but not its update mechanism;
   without `onupdate`, this column would only ever reflect the row's
   FIRST write (identical to `created_at`) and never actually track "most
   recently updated," defeating its own name. `server_default=func.now()`
   on first insert, same posture as `created_at`.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Index, String, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class VideoPlaybackLog(Base):
    """One row per (user, video) pair -- this student's cumulative
    engagement with this one video, across every heartbeat pulse ever
    sent for it. Fully specified by this milestone's own ask; see this
    module's own docstring for the disclosed judgment calls behind the
    schema choices below.
    """

    __tablename__ = "video_playback_logs"
    __table_args__ = (
        UniqueConstraint("user_id", "video_id", "video_type", name="uq_video_playback_log_user_video"),
        # Composite lookup index covering the heartbeat endpoint's own
        # `SELECT ... FOR UPDATE WHERE user_id = ? AND video_id = ? AND
        # video_type = ?` -- the unique constraint above already implies
        # a covering index for exactly this column order in Postgres, so
        # this is technically redundant with it, but named and kept
        # explicit (rather than relying on an implicit side effect of the
        # constraint) for the same "be explicit about the access pattern
        # this index exists for" discipline `student_videos.py` already
        # established for its own composite index.
        Index("ix_video_playback_logs_user_video", "user_id", "video_id", "video_type"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    # Nullable -- see module docstring, judgment call 2.
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=True, index=True
    )

    # Polymorphic, unconstrained -- see module docstring, judgment call 1.
    video_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)

    # Plain String, backed by `VideoType` (app/models/enums.py) and
    # enforced at the Pydantic layer -- same "status-shaped column stays
    # a plain String unless the ask explicitly wants a native Postgres
    # ENUM type" convention as every other lifecycle-style column in this
    # codebase (`StudentVideo.status`, `Lesson.video_processing_status`,
    # ...). This ask's own literal spec says `video_type: String`, which
    # only reinforces the same default.
    video_type: Mapped[str] = mapped_column(String(20), nullable=False)

    last_position_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    cumulative_watch_time_seconds: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<VideoPlaybackLog id={self.id} user_id={self.user_id} video_id={self.video_id} "
            f"video_type={self.video_type!r} is_completed={self.is_completed}>"
        )
