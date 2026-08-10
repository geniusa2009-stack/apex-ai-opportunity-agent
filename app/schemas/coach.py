from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class DashboardOut(BaseModel):
    """GET /coach/dashboard response -- "tokens balance, streak, and
    current status" per the ask, read as the full StudentAccountability
    record (balance + streak + procrastination score + last activity)
    rather than a narrower subset, since all four are exactly what
    "current status" means for an accountability record."""

    model_config = ConfigDict(from_attributes=True)

    user_id: UUID
    tokens_balance: int
    total_earned_tokens: int
    current_streak: int
    last_active_at: datetime | None
    procrastination_score: int


class DailyClaimOut(BaseModel):
    tokens_awarded: int
    tokens_balance: int
    current_streak: int
