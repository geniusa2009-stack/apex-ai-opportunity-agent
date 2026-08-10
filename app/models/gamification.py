import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Integer
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class StudentAccountability(Base):
    """One row per student, tracking their AI Accountability Coach &
    Premium Token Economy state (Ecosystem Feature 4). `user_id` is BOTH
    the primary key and the foreign key to `users.id` -- a genuine 1:1
    relationship, unlike every other model in this codebase (which all
    have their own UUID `id` primary key plus a separate indexed
    `user_id` foreign key). This is exactly how the ask specified this
    one column ("user_id: UUID (PrimaryKey, ForeignKey to users.id)"),
    followed literally rather than defaulting to this project's usual
    shape, since a student accountability record is conceptually a
    1:1 extension of `User` itself, not an independent many-per-user
    resource the way e.g. `UserHistory` or `DeadlineAlert` are.

    No timestamp columns at all (`created_at` included) -- the ask's
    field list for this model is exhaustive and lists none. Every prior
    model in this project's Ecosystem tier at least kept an explicit
    `created_at`; this is the first one where even that was omitted from
    the ask, so it's omitted here too rather than assumed.

    `tokens_balance`/`total_earned_tokens` both default to 100 (free
    starter tokens, per the ask) -- the same number, since a brand-new
    account's balance and lifetime-earned total are identical until the
    first award or deduction ever happens.
    """

    __tablename__ = "student_accountability"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    tokens_balance: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    total_earned_tokens: Mapped[int] = mapped_column(Integer, default=100, nullable=False)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_active_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # Live snapshot of currently-overdue milestones, recomputed each time
    # `coach_service.generate_pep_talk` runs -- see that function's
    # comment for the disclosed interpretation of this column's terse
    # one-line ask description ("increases if tasks in Kanban remain
    # overdue").
    procrastination_score: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    def __repr__(self) -> str:
        return (
            f"<StudentAccountability user_id={self.user_id} "
            f"tokens_balance={self.tokens_balance} streak={self.current_streak}>"
        )
