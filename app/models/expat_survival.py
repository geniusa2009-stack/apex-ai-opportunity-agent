import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class ExpatSurvival(Base):
    """A running or completed "AI Expat RPG" session (Ecosystem Feature
    10: AI Expat RPG & Cultural Landmine Game).

    IMPORTANT DISCLOSURE, same posture as Features 5, 6, and 8's
    equivalent notes: this step's ask was framed as an *upgrade* to a
    pre-existing "Ecosystem Feature 10" base game -- "keep basic fields,
    but update/add," and the router spec references `target_city`/
    `target_country` as though they already existed. A repo search
    (`Grep` for `expat_survival|ExpatSurvival|Expat Survival|Expat RPG`,
    case-insensitive, across the whole codebase and README) turned up
    zero matches -- no such feature was ever actually built here.

    This is the fourth time this exact pattern has occurred this session
    (after Features 5, 6, and 8). Per the standing resolution established
    the first time this came up and confirmed as the default since, it's
    applied directly here again without re-asking: everything below
    marked **BASELINE** is this project's own reconstruction (not
    restored real prior work). Everything marked **UPGRADE** is this
    step's literal, explicit ask, built on top of that reconstruction.
    """

    __tablename__ = "expat_survivals"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- BASELINE (this project's own reconstruction -- see the class
    # docstring above). `target_city`/`target_country` are named
    # directly by this step's own router spec ("Accepts `target_city`,
    # `target_country`"), so they had to already exist on the model for
    # that wording to make sense. `is_completed`/`final_survival_score`/
    # `tokens_earned` round out the minimal state a turn-based game
    # session needs -- the ask's `continue_rpg_session` description
    # computes a final score and awards tokens but never names a column
    # to persist either in, so these had to be invented for that
    # end-game behavior to have anywhere to land. ---
    target_city: Mapped[str] = mapped_column(String(255), nullable=False)
    target_country: Mapped[str] = mapped_column(String(255), nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    final_survival_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    tokens_earned: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- UPGRADE (this step's literal ask, "keep basic fields, but
    # update/add:") ---
    current_step: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    # List of {"role": "ai"/"user", "text": "..."}, in chronological order.
    chat_history: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    # List of {"word": str, "phonetic_arabic": str, "usage_context": str}.
    # Not `nullable=True` despite the ask's field description not
    # explicitly stating nullability (unlike `chat_history`/
    # `cultural_landmine_triggered`, which do) -- it's always generated
    # and populated in the same `start_rpg_session` call that creates
    # this row, so there's no real window where a row would exist
    # without one.
    slang_dictionary: Mapped[list[dict]] = mapped_column(JSONB, nullable=False)
    cultural_landmine_triggered: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return (
            f"<ExpatSurvival id={self.id} user_id={self.user_id} "
            f"current_step={self.current_step} is_completed={self.is_completed}>"
        )
