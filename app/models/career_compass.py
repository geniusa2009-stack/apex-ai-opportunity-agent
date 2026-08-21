"""Career Compass -- a real, deterministic RIASEC (Holland Code) interest
inventory. Built from scratch: the prior investigation confirmed nothing
in this codebase (no route, no model column, no service) implements any
form of career/interest matching -- grepped whole-app for career/compass/
interest/match_percentage/personality/aptitude, read every plausibly-
adjacent endpoint file in full, and found zero real hits (see that
investigation's own report for the exact commands and output).

DISCLOSED METHODOLOGY CHOICE (Step 2 of the ask, deliberately not an LLM
call): this is Option A -- a real, well-established psychometric
methodology (Holland's RIASEC/"Holland Code" model, the same six-
dimension framework behind O*NET's Interest Profiler and the Strong
Interest Inventory), scored by a pure, deterministic function
(`app/services/career_compass_service.py::score_riasec`) with no AI
gateway call at all. Chosen over an LLM-graded approach (the
`skill_gap.py`/`test_pathway.py`/`probability.py` pattern) specifically
BECAUSE a "match percentage" a student sees is more defensible when it's
a real, reproducible count of how many times they picked each dimension
out of 10 questions than when it's an LLM's free-form guess dressed up as
a number -- see that service module's own docstring for the exact scoring
rule, and `quiz.html`'s own UI copy (app.frontend repo) for how this is
disclosed to the student. Deliberately does NOT factor in `User.major`/
`target_country`/`skills` -- RIASEC is an answers-only interest inventory
by design; blending in profile fields would silently turn a real
psychometric score into something else without disclosing it.

DISCLOSED PERSISTENCE DECISION: one row per student, `user_id` as BOTH
primary key and foreign key -- the exact same 1:1-with-User shape
`GamificationProfile` (app/models/badges.py) already established in this
codebase. A submission OVERWRITES the previous row (upsert), matching
the product's own "Retake the quiz" affordance already visible in
`quiz.html`'s pre-existing (until now, non-functional) markup -- this is
a CURRENT result a student can revisit, not a full submission history
(nothing in the ask or the UI implies a student needs to compare past
attempts over time; building that would be un-asked-for scope).
"""

import datetime as dt
import uuid

from sqlalchemy import DateTime, ForeignKey, Float, String, func
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class CareerCompassResult(Base):
    """One student's current RIASEC result. See this module's own
    docstring for the full disclosed methodology/persistence reasoning.

    `answers` -- the raw 10 submitted answer indices (0-5 each, one per
    question), stored verbatim so a past result can be explained/audited
    later without needing to have kept a separate log.

    `dimension_scores` -- a JSON object of the real per-dimension raw
    counts, e.g. `{"R": 1, "I": 3, "A": 0, "S": 2, "E": 3, "C": 1}` --
    always sums to exactly 10 (one point per question). Stored (not just
    derived on read) so `GET /career-compass/me` doesn't need to re-run
    the scoring function against the raw answers just to render the same
    numbers again.

    `holland_code` -- the top 3 dimension letters by score, descending,
    ties broken by the fixed R-I-A-S-E-C order (e.g. `"IEA"`). Standard
    RIASEC reporting convention.

    `matched_careers` -- JSON list of `{"name_ar": ..., "name_en": ...}`
    objects for the TOP-SCORING dimension(s) only (ties included) -- see
    the service module's own docstring for why this codebase's first
    real version maps from the single top dimension rather than building
    a full 15-combination two-letter mapping table.
    """

    __tablename__ = "career_compass_results"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    answers: Mapped[list] = mapped_column(JSONB, nullable=False)
    dimension_scores: Mapped[dict] = mapped_column(JSONB, nullable=False)
    holland_code: Mapped[str] = mapped_column(String(6), nullable=False)
    match_percentage: Mapped[float] = mapped_column(Float, nullable=False)
    matched_careers: Mapped[list] = mapped_column(JSONB, nullable=False)

    created_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<CareerCompassResult user_id={self.user_id} holland_code={self.holland_code!r}>"
