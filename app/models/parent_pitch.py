import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ParentPitch(Base):
    """A generated "Parent Pitch" -- a persuasion strategy + formal Arabic
    reassurance letter helping a student make the case to their family for
    studying abroad (Ecosystem Feature 7: AI Parent Pitcher & Reassurance
    Portal).

    `primary_concern` is a plain `String`, not a native Postgres ENUM or
    even a project-local Python enum enforced at the Pydantic layer
    (unlike `TrackerStatus`/`FocusSessionStatus` elsewhere in this
    codebase) -- the ask's own field description uses "e.g." before its
    four examples ("safety", "finance", "culture", "distance"), signaling
    illustrative examples rather than an exhaustive fixed set the way
    `ApplicationTracker.status`'s ask enumerated every allowed value.
    Locking this down to exactly those four strings would wrongly reject
    a genuinely different concern a family might raise (e.g. "religious
    environment," "career prospects," "language barrier"). See
    `app/schemas/parent_pitch.py` for where this is validated instead
    (free text with a sane length bound).

    `generated_pitch_data` stores the full structured Gemini output as
    JSONB (matching `ApplicationTracker.ai_todo_list`'s precedent for
    "AI JSON output, persisted as-is") -- its shape is `GeneratedPitchData`
    in app/schemas/parent_pitch.py.

    No `updated_at` -- same literal-field-list posture already
    established repeatedly in this project's Ecosystem tier: the ask
    names exactly `id`, `user_id`, `scholarship_name`, `target_country`,
    `primary_concern`, `generated_pitch_data`, `created_at`, with no
    `updated_at`. A pitch is generated once and read thereafter, not
    edited in place, which also fits a create-only, no-`updated_at` shape
    naturally.
    """

    __tablename__ = "parent_pitches"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    scholarship_name: Mapped[str] = mapped_column(String(500), nullable=False)
    target_country: Mapped[str] = mapped_column(String(255), nullable=False)
    primary_concern: Mapped[str] = mapped_column(String(100), nullable=False)

    generated_pitch_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ParentPitch id={self.id} user_id={self.user_id} "
            f"scholarship_name={self.scholarship_name!r} primary_concern={self.primary_concern!r}>"
        )
