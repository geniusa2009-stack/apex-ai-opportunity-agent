from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class StartSessionRequest(BaseModel):
    """POST /focus/start request body. `declared_goal`/`subject` are the
    two fields this step's ask's `start_session` description explicitly
    references by name; `duration_minutes` is this project's own
    reconstructed baseline field (see app/models/focus_room.py), exposed
    here as an optional override of the classic 25-minute Pomodoro
    default rather than hardcoded."""

    declared_goal: str = Field(min_length=1, max_length=500)
    subject: str = Field(min_length=1, max_length=255)
    duration_minutes: int = Field(default=25, ge=5, le=120)


class FocusSessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    declared_goal: str
    subject: str
    duration_minutes: int
    status: str
    started_at: datetime
    ended_at: datetime | None
    tokens_earned: int
    study_summary: str | None
    quiz_question: str | None
    student_answer: str | None
    quiz_passed: bool | None
    suggested_soundscape: str | None


class StartSessionOut(FocusSessionOut):
    """`POST /focus/start` response: the created session plus the
    ephemeral `brain_break_activity` suggestion, which -- per
    app/models/focus_room.py's docstring -- is deliberately NOT a
    persisted column, only ever returned in this one response."""

    brain_break_activity: str


class EndBlockRequest(BaseModel):
    """POST /focus/end-block/{session_id} request body."""

    study_summary: str = Field(min_length=1, max_length=5000)


class EndBlockOut(BaseModel):
    """POST /focus/end-block/{session_id} response: just enough to drive
    the next step (submitting an answer) -- the full session object is
    also available via the session's own fields if a caller wants it."""

    session_id: UUID
    quiz_question: str


class SubmitQuizRequest(BaseModel):
    """POST /focus/submit-quiz/{session_id} request body."""

    student_answer: str = Field(min_length=1, max_length=2000)


class SubmitQuizOut(BaseModel):
    """POST /focus/submit-quiz/{session_id} response."""

    session_id: UUID
    quiz_passed: bool
    proctor_feedback: str
    tokens_earned: int
    tokens_balance: int


class AbandonSessionOut(BaseModel):
    """POST /focus/abandon/{session_id} response."""

    session_id: UUID
    status: str
