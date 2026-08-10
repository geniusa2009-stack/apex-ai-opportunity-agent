from uuid import UUID

from pydantic import BaseModel, Field


class StartRequest(BaseModel):
    """POST /expat-survival/start request body."""

    target_city: str = Field(min_length=1, max_length=255)
    target_country: str = Field(min_length=1, max_length=255)


class SlangEntryOut(BaseModel):
    word: str
    phonetic_arabic: str
    usage_context: str


class StartSessionOut(BaseModel):
    """POST /expat-survival/start response: the ask says "Returns slang
    dict and step 1 scenario" -- includes just enough session identity
    (`session_id`, `current_step`) for the client to then call
    `POST /expat-survival/play/{id}`."""

    session_id: UUID
    target_city: str
    target_country: str
    current_step: int
    slang_dictionary: list[SlangEntryOut]
    opening_scenario: str


class PlayRequest(BaseModel):
    """POST /expat-survival/play/{id} request body."""

    student_input: str = Field(min_length=1, max_length=2000)


class PlayTurnOut(BaseModel):
    """POST /expat-survival/play/{id} response -- the ask says "Returns
    the next turn of the story, whether a landmine was triggered, and
    (if it was the final step) the final evaluation and earned tokens."
    `final_survival_score`/`tokens_earned`/`tokens_balance` stay `None`
    on any non-final turn."""

    session_id: UUID
    current_step: int
    next_turn_narrative: str
    landmine_triggered_this_turn: bool
    landmine_explanation: str | None
    is_completed: bool
    final_survival_score: int | None
    tokens_earned: int | None
    tokens_balance: int | None
