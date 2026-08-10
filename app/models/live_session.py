"""Live 1:1 Video Sessions -- `LiveSession` model.

Feature #1 of ten "take the app somewhere new" proposals (offered in
response to an open-ended "suggest 10 features that would move the app to
a whole new level" ask, then picked to build first). Every prior AI
feature in this codebase is either asynchronous (a Gemini call that
returns text/JSON) or, at most, a persisted text chat -- nothing lets two
real people actually see and talk to each other live. This model is the
booking/lifecycle record behind a real WebRTC video call between two
users (e.g. a mentor and mentee via the existing `/mentorship` feature, or
two peers practicing a mock interview) -- the signaling itself (SDP
offer/answer, ICE candidates) is NOT stored here; it's relayed live over a
WebSocket and never touches the database (see `app/api/v1/endpoints/
live_sessions.py`'s WebSocket handler and `app/services/
live_session_connection_manager.py`).

DISCLOSED JUDGMENT CALL -- direction is NOT fixed the way `MentorshipRelation`
fixes it ("the mentee always initiates"). A live session is a general-
purpose booking between any two users -- a mentee booking time with their
mentor, or two peers scheduling a mock-interview practice call -- so
either `requested_by` or `requested_with` could sensibly be either role.
The model just tracks "who proposed this" (`requested_by`) vs. "who has to
accept it" (`requested_with`), with no assumption about which of the two
is more senior/expert.

DISCLOSED JUDGMENT CALL -- rating/feedback are single-sided (whoever calls
`POST .../complete` first may attach them; the other party's own
retrospective view of the session, if they call `/complete` on an already-
COMPLETED session, is rejected as an invalid transition rather than
collecting a second rating). This mirrors a limitation this codebase's own
README already discloses for `StudyDuel` ("Study Duels' single-sided
`complete` report" is explicitly named there as a known, not-yet-addressed
gap) -- reusing that same known trade-off here rather than either building
a heavier dual-confirmation flow speculatively or silently pretending
single-sided rating has no downside.
"""

import datetime as dt
import uuid

from sqlalchemy import Boolean, CheckConstraint, DateTime, ForeignKey, Integer, String, Text
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import LiveSessionStatus


class LiveSession(Base):
    """A booked (and, once `CONFIRMED`, joinable) real-time video call
    between exactly two users.

    No `relationship()` objects to `User` -- same "no speculative eager-
    loaded collections nothing yet reads" posture already established for
    `UserEnrollment`/`LessonProgress`/`MentorshipRelation`/`Certificate`
    (all FK-columns-only; the other party's `User` row is fetched
    explicitly by the router when building a response, exactly like
    `mentorship.py`'s own `_to_out` helper).
    """

    __tablename__ = "live_sessions"
    __table_args__ = (
        CheckConstraint("requested_by_id != requested_with_id", name="ck_live_session_distinct_parties"),
        CheckConstraint(
            "rating IS NULL OR (rating >= 1 AND rating <= 5)", name="ck_live_session_rating_range"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    requested_by_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    requested_with_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # Plain-String-backed `LiveSessionStatus` -- see that enum's own
    # docstring (app/models/enums.py) for the full state-machine
    # reasoning and why it has five values, not three.
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default=LiveSessionStatus.REQUESTED.value
    )

    # The proposed call time, set by `requested_by` at booking time.
    # Deliberately NOT validated at the database layer to be in the
    # future (Postgres has no simple portable "compare to NOW() at write
    # time" CHECK) -- that validation lives at the service layer
    # (`live_session_service.request_session`), same layering as every
    # other business-rule check in this codebase.
    scheduled_at: Mapped[dt.datetime] = mapped_column(DateTime(timezone=True), nullable=False)

    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=30)

    # What the session is about -- e.g. "Mock interview practice",
    # "Monthly mentor check-in." Free text, nullable (a session is
    # bookable with no label at all).
    topic: Mapped[str | None] = mapped_column(String(255), nullable=True)

    requested_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=lambda: dt.datetime.now(dt.timezone.utc)
    )
    # Touched by respond/cancel/complete -- whichever state-changing
    # action happens AFTER the initial request. Reuses one column for
    # "when did this row's status last change post-creation" rather than
    # a separate timestamp per possible transition (`responded_at`,
    # `cancelled_at`, `completed_at` all collapsing into the same
    # meaning) -- mirrors `MentorshipRelation.responded_at`'s own reuse
    # across BOTH its `respond` and `complete` transitions.
    responded_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Single-sided rating/feedback of the session, set only via
    # `POST .../complete` -- see this module's own docstring for the
    # disclosed single-sided-rating trade-off.
    rating: Mapped[int | None] = mapped_column(Integer, nullable=True)
    feedback: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Scheduled-call reminder tracking -- added by the "Live Session
    # Reminders" follow-up step, reusing `DeadlineAlert.reminder_sent`'s
    # OWN exact column shape (`Boolean, default=False`) rather than
    # inventing a new "reminded" representation: same problem
    # (a periodic Celery Beat scan must not re-notify a row it already
    # notified on a previous run), same fix. See `app/workers/
    # live_session_reminder_tasks.py`'s own module docstring for the full
    # "why a periodic task at all" reasoning, which is itself a direct
    # reuse of `app/workers/deadline_reminder_tasks.py`'s own reasoning.
    reminder_sent: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    def __repr__(self) -> str:
        return (
            f"<LiveSession id={self.id} requested_by_id={self.requested_by_id} "
            f"requested_with_id={self.requested_with_id} status={self.status}>"
        )
