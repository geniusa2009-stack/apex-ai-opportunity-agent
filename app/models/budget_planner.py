import datetime as dt
import uuid

from sqlalchemy import DateTime, Float, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class BudgetPlanner(Base):
    """A generated cost-of-living/budget simulation (Ecosystem Feature 9:
    AI Smart Budget & Cost of Living Simulator).

    `generated_budget_data` stores the full structured Gemini output as
    JSONB (matching `ApplicationTracker.ai_todo_list`/`ParentPitch.
    generated_pitch_data`/`DecisionMatrix.matrix_result`'s established
    precedent for "AI JSON output, persisted as-is") -- its shape is
    `GeneratedBudgetData` in app/services/budget_service.py.

    No `updated_at` -- same literal-field-list posture already
    established repeatedly in this project's Ecosystem tier: the ask
    names exactly `id`, `user_id`, `target_city`, `target_country`,
    `monthly_stipend`, `generated_budget_data`, `created_at`, with no
    `updated_at`. A budget simulation is generated once and read/deleted
    thereafter, not edited in place.
    """

    __tablename__ = "budget_planners"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    target_city: Mapped[str] = mapped_column(String(255), nullable=False)
    target_country: Mapped[str] = mapped_column(String(255), nullable=False)
    monthly_stipend: Mapped[float] = mapped_column(Float, nullable=False, default=0.0)

    generated_budget_data: Mapped[dict] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<BudgetPlanner id={self.id} user_id={self.user_id} "
            f"target_city={self.target_city!r} target_country={self.target_country!r}>"
        )
