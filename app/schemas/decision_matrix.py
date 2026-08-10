from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class AnalyzeRequest(BaseModel):
    """POST /decision/analyze request body -- exactly the fields this
    step's ask names: `decision_topic`, `options_list`,
    `student_priorities`, `options_gut_feelings` (dict), and
    `is_reality_check_enabled` (boolean)."""

    decision_topic: str = Field(min_length=1, max_length=500)
    options_list: list[str] = Field(min_length=2, max_length=10)
    student_priorities: dict[str, float] = Field(min_length=1)
    options_gut_feelings: dict[str, float] | None = None
    is_reality_check_enabled: bool = False


class OptionScoreOut(BaseModel):
    option: str
    logical_score: float
    emotional_score: float
    combined_score: float


class FutureSimulationOut(BaseModel):
    option: str
    narrative: str


class RealityCheckOut(BaseModel):
    option: str
    hidden_struggles: list[str]


class MatrixResultOut(BaseModel):
    """Mirrors `app.services.decision_service.GeneratedMatrixResult` --
    kept as a separate schema-layer copy, same rationale already
    established in `app/schemas/parent_pitch.py`'s `GeneratedPitchData`:
    Gemini's structured-output contract and this endpoint's public
    response contract are allowed to drift independently."""

    weighted_scores: list[OptionScoreOut]
    future_simulations: list[FutureSimulationOut]
    reality_checks: list[RealityCheckOut]
    the_verdict: str


class MatrixOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    decision_topic: str
    options_list: list[str]
    student_priorities: dict[str, float]
    options_gut_feelings: dict[str, float] | None
    is_reality_check_enabled: bool
    matrix_result: MatrixResultOut
    created_at: datetime
