import datetime as dt
import uuid

from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import TrackerStatus

if TYPE_CHECKING:
    from app.models.opportunity import Opportunity


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
    __table_args__ = (
        # Application Intelligence step, Part T: duplicate-application
        # protection enforced at BOTH the application layer
        # (`application_tracker_service.create_tracker`'s pre-insert
        # check) AND the database layer (this partial unique index) --
        # only when `opportunity_id` is actually set, since a
        # freeform/off-catalog tracker (no `opportunity_id`) has no
        # meaningful notion of "duplicate" to enforce (a student can
        # legitimately track two different off-catalog scholarships that
        # happen to share a name). Postgres treats each NULL as distinct
        # for uniqueness purposes even without a partial index, but the
        # explicit `postgresql_where` keeps the *intent* -- "one tracker
        # per (user, catalog opportunity)" -- visible in the schema
        # itself, not just an accidental NULL-handling side effect.
        Index(
            "uq_application_trackers_user_opportunity",
            "user_id", "opportunity_id",
            unique=True,
            postgresql_where="opportunity_id IS NOT NULL",
        ),
    )

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

    # Application Intelligence step, Part A: the User -> Application ->
    # Opportunity link this phase's own audit found genuinely missing.
    # Nullable -- a tracker MAY still target a scholarship that doesn't
    # exist in this platform's own catalog (unchanged, pre-existing
    # capability, Part 0's own audit finding), in which case this stays
    # NULL and the deterministic, evidence-traceable requirements/
    # deadline-intelligence/change-impact features that depend on a real
    # `Opportunity` row simply have nothing to project (never fabricated
    # -- see `application_requirements.py`). `ondelete="SET NULL"`, not
    # CASCADE -- deleting the catalog `Opportunity` row must never delete
    # the user's own tracked application record; it only loses its
    # catalog link.
    opportunity_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("opportunities.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )

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

    # Application Intelligence step, Part M (Submission Safety): the
    # explicit, real submission record. NULL until
    # `application_tracker_service.submit_tracker` runs -- the ONLY
    # write path that ever sets this column (never the generic
    # `PATCH /tracker/{id}/status`, per Part U). Its presence, not
    # `status == "applied"` alone, is what this codebase treats as "the
    # user explicitly confirmed they submitted this application" --
    # `status` could in principle be set to "applied" through data
    # migration/backfill someday without this column being set, and this
    # column is the one deliberately-narrow signal nothing else can
    # produce as a side effect.
    submitted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    opportunity: Mapped["Opportunity | None"] = relationship(lazy="selectin")

    def __repr__(self) -> str:
        return (
            f"<ApplicationTracker id={self.id} user_id={self.user_id} "
            f"scholarship_name={self.scholarship_name!r} status={self.status!r}>"
        )
