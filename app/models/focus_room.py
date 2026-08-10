import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import FocusSessionStatus


class FocusSession(Base):
    """One Pomodoro-style focus block a student runs (Ecosystem Feature 5:
    AI Multi-Sensory Study Buddy & Distraction Shield).

    IMPORTANT DISCLOSURE, read this before trusting any field below as
    "already decided" product spec: this step's ask was framed as an
    *upgrade* to a pre-existing "Ecosystem Feature 5: AI Pomodoro & Focus
    Rooms" -- it says "keep the previous fields," and its `start_session`
    description references a `declared_goal` field as though it already
    existed. A full repo search (Glob for `*focus*`/`*pomodoro*` files,
    Grep for `Ecosystem Feature 5|Pomodoro|Focus Room|declared_goal`
    across the whole codebase and README) turned up zero matches -- no
    such feature was ever actually built here. Given the user's explicit
    choice (via `AskUserQuestion`) to "build the base feature first, then
    the upgrade," everything in the block below marked BASELINE is this
    project's own reconstruction of what that prerequisite feature
    plausibly looked like, invented for this step and disclosed as such
    (NOT a restoration of real prior work). The block marked UPGRADE is
    the literal, explicit field list from this step's actual ask.

    Baseline design choices, disclosed:
    - `declared_goal` / `subject`: both referenced by name in the
      upgrade ask's `start_session` description ("matching their
      `declared_goal` and study subject") -- so these two, at minimum,
      had to exist on the baseline model for the upgrade's own wording
      to make sense. Kept as required (`nullable=False`) since a Pomodoro
      session without a stated goal/subject has nothing for the AI
      suggestions (or the active-recall quiz) to anchor to.
    - `duration_minutes`: classic Pomodoro technique default is 25
      minutes; exposed as a caller-overridable field (see
      `app/schemas/focus.py`'s `StartSessionRequest`) rather than
      hardcoded, since "Pomodoro" is a convention, not a hard rule.
    - `status`: session lifecycle (`FocusSessionStatus` --
      in_progress/completed/abandoned), needed for the three named
      lifecycle endpoints (start/end-block+submit-quiz/abandon) to have
      somewhere to record which one last happened to a given session.
    - `started_at`/`ended_at`/`tokens_earned`: minimal bookkeeping any
      session-based, token-earning feature needs -- `started_at` doubles
      as this model's only creation timestamp (no separate `created_at`,
      to avoid a redundant column), `ended_at` is set on completion OR
      abandonment, `tokens_earned` records what THIS session actually
      paid out (0 until `verify_quiz_answer` runs).

    Upgrade fields (this step's literal, explicit ask) all follow directly
    below the baseline fields, each `nullable=True` exactly as specified.
    Notably NOT included as a column: the "AI Brain Break Activity"
    `start_session` also generates -- the ask's upgrade field list names
    `suggested_soundscape` but not a brain-break column, so that value is
    treated as ephemeral (returned once in the `POST /focus/start`
    response, never persisted) rather than silently added as an extra
    column beyond the ask's literal list.
    """

    __tablename__ = "focus_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- BASELINE (this project's own reconstruction -- see the class
    # docstring above) ---
    declared_goal: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    duration_minutes: Mapped[int] = mapped_column(Integer, nullable=False, default=25)
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=FocusSessionStatus.IN_PROGRESS.value
    )
    started_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    ended_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tokens_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    # --- UPGRADE (this step's literal ask -- "keep the previous fields,
    # and add:") ---
    study_summary: Mapped[str | None] = mapped_column(Text, nullable=True)
    quiz_question: Mapped[str | None] = mapped_column(Text, nullable=True)
    student_answer: Mapped[str | None] = mapped_column(Text, nullable=True)
    quiz_passed: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    suggested_soundscape: Mapped[str | None] = mapped_column(String(255), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<FocusSession id={self.id} user_id={self.user_id} "
            f"subject={self.subject!r} status={self.status!r}>"
        )
