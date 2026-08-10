import datetime as dt
import uuid

from sqlalchemy import Boolean, DateTime, ForeignKey, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class DecisionMatrix(Base):
    """A generated scholarship/path decision analysis (Ecosystem Feature
    8: AI Multidimensional Path Simulator & Reality Checker).

    IMPORTANT DISCLOSURE, same posture as `app/models/focus_room.py`
    (Feature 5) and `app/models/guilds.py` (Feature 6): this step's ask
    was framed as an *upgrade* to a pre-existing "Ecosystem Feature 8:
    The Scholarship Decision Matrix" -- "keep the basic columns, and
    add/update," and the router spec references `decision_topic`,
    `options_list`, and `student_priorities` as though they already
    existed. A repo search (`Grep` for `decision_matrix|DecisionMatrix|
    Decision Matrix|decision_matrices`, case-insensitive, across the
    whole codebase and README) turned up zero matches -- no such feature
    was ever actually built here.

    This is now the third time this exact pattern has occurred this
    session (after Feature 5's missing "AI Pomodoro & Focus Rooms" and
    Feature 6's missing "Study Guilds"), each resolved by reconstructing
    a sensible baseline myself, disclosed clearly, per your explicit
    choice the first time this came up. That same resolution is applied
    directly here again, without re-asking, for the reasons given in
    Feature 6's README section. Everything below marked **BASELINE** is
    this project's own reconstruction (not restored real prior work).
    Everything marked **UPGRADE** is this step's literal, explicit ask.
    """

    __tablename__ = "decision_matrices"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )

    # --- BASELINE (this project's own reconstruction -- see the class
    # docstring above). All three are named directly by this step's own
    # router spec ("Accepts `decision_topic`, `options_list`,
    # `student_priorities`..."), so they had to already exist on the
    # model for that wording to make sense. ---
    decision_topic: Mapped[str] = mapped_column(String(500), nullable=False)
    # List of option names being compared, e.g. ["Germany", "Turkey"].
    # Stored as JSONB (matching this codebase's established convention
    # for AI-adjacent list/dict data -- e.g. `ApplicationTracker.ai_todo_list`
    # -- rather than introducing this project's first use of Postgres
    # ARRAY columns for a single new feature).
    options_list: Mapped[list[str]] = mapped_column(JSONB, nullable=False)
    # Dict of priority name -> weight/importance, e.g. {"cost": 8,
    # "career_prospects": 9} -- the "logical priorities" the upgrade
    # ask's service description refers to.
    student_priorities: Mapped[dict] = mapped_column(JSONB, nullable=False)
    # The full structured AI output. Already existed in the reconstructed
    # baseline (holding, presumably, just `weighted_scores`); this step's
    # ask says to update its *shape* to also include
    # `future_simulations`/`reality_checks`/`the_verdict` -- no new
    # column needed for that, just a richer JSON payload in the same
    # field. Current shape: `app.services.decision_service.GeneratedMatrixResult`.
    matrix_result: Mapped[dict] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    # --- UPGRADE (this step's literal ask, "keep the basic columns, and
    # add/update:") ---
    # Dict of option name -> emotional score 1-10, e.g. {"Germany": 9,
    # "Turkey": 5}. Nullable -- gut feelings are optional student input.
    options_gut_feelings: Mapped[dict | None] = mapped_column(JSONB, nullable=True)
    is_reality_check_enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    def __repr__(self) -> str:
        return (
            f"<DecisionMatrix id={self.id} user_id={self.user_id} "
            f"decision_topic={self.decision_topic!r}>"
        )
