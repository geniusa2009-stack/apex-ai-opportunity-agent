import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import TrackerStatus


class ApplicationTracker(Base):
    """One scholarship a student is tracking through a Kanban-style
    application workflow (Ecosystem Feature 1: AI-Powered Application
    Kanban Tracker). Distinct from `UserBookmark` (a lightweight "saved
    for later" pointer to a catalog `Opportunity` row) -- a tracker can
    be created for ANY scholarship the student names, whether or not it
    exists in this platform's own `opportunities` catalog, and carries a
    whole AI-generated preparation checklist alongside it.

    No `updated_at`/`TimestampMixin` -- same literal-field-list posture
    already established for `UserHistory`/`StudentMemory`
    (app/models/user_history.py): the ask names exactly `id`, `user_id`,
    `scholarship_name`, `institution`, `status`, `deadline`, `notes`,
    `ai_todo_list`, `created_at` for this model, with no `updated_at`.
    """

    __tablename__ = "application_trackers"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    scholarship_name: Mapped[str] = mapped_column(String(500), nullable=False)
    institution: Mapped[str | None] = mapped_column(String(255), nullable=True)

    # Plain String, NOT a native Postgres ENUM (unlike
    # OpportunityType/BookmarkStatus elsewhere in this codebase) -- this
    # step's ask explicitly types this column as `String` ("Enum-like
    # values" describes the semantics of what's allowed, not an
    # instruction to use a DB-level enum type), distinct wording from how
    # the earlier Enum-typed columns were asked for. `TrackerStatus`
    # (app/models/enums.py) is still the single source of truth for the
    # allowed values -- enforced at the Pydantic layer
    # (app/schemas/application_tracker.py's `TrackerStatusUpdate`)
    # instead of a DB-level type/constraint.
    status: Mapped[str] = mapped_column(
        String(30), nullable=False, default=TrackerStatus.INTERESTED.value
    )

    deadline: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    # List of {"task_id": int, "task_name": str, "is_completed": bool}.
    # Gemini only ever generates the `task_name` values (see
    # app/services/application_tracker.py's `_GeneratedTodoList`) --
    # `task_id` (sequential, assigned at creation) and `is_completed`
    # (always False initially) are added by our own code before this
    # column is ever written, never by the model.
    ai_todo_list: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<ApplicationTracker id={self.id} user_id={self.user_id} "
            f"scholarship_name={self.scholarship_name!r} status={self.status!r}>"
        )
