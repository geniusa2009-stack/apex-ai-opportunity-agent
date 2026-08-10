from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class PitchGenerateRequest(BaseModel):
    """POST /parent-pitch/generate request body.

    `primary_concern` is deliberately plain free text (not a hardcoded
    `Literal`, unlike `TrackerStatusUpdate.new_status` elsewhere in this
    codebase) -- see app/models/parent_pitch.py's docstring for why:
    the ask's own four examples are introduced with "e.g.," signaling
    illustrative examples rather than an exhaustive fixed set.
    """

    scholarship_name: str = Field(min_length=1, max_length=500)
    target_country: str = Field(min_length=1, max_length=255)
    primary_concern: str = Field(min_length=1, max_length=100)


class GeneratedPitchData(BaseModel):
    """Mirrors `app.services.parent_pitch_service.GeneratedPitchData` --
    kept as a separate schema-layer copy (rather than importing the
    service's Pydantic model directly into the API layer) for the same
    reason the rest of this codebase keeps `app/schemas/*` independent of
    `app/services/*` internals: the service's model is Gemini's
    structured-output contract, this one is this endpoint's public
    response contract, and letting them drift independently is safer
    than coupling them permanently.
    """

    conversation_tactics: list[str]
    key_reassurance_points: list[str]
    financial_reassurance: str
    formal_arabic_letter: str


class PitchOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    scholarship_name: str
    target_country: str
    primary_concern: str
    generated_pitch_data: GeneratedPitchData
    created_at: datetime
