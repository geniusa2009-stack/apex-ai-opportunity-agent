from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class HistoryCreate(BaseModel):
    """POST /history/ request body. `payload` is the full, arbitrary JSON
    artifact content (whatever shape the originating feature produced) --
    intentionally untyped (`dict[str, Any]`) since this endpoint is
    feature-agnostic by design; it doesn't know or care which of this
    codebase's many Gemini/Claude features the payload came from."""

    feature_key: str = Field(max_length=100, description="Which feature produced this artifact, e.g. 'essay_sparker'.")
    title: str = Field(max_length=500)
    payload: dict[str, Any]
    is_pinned: bool = False


class HistoryUpdate(BaseModel):
    """PATCH /history/{id} request body. Only the fields a caller could
    plausibly want to change after creation -- `feature_key`,
    `storage_path`, and the artifact content itself are immutable by
    design (editing saved history is out of scope for this step; see the
    README's Not Included list)."""

    title: str | None = Field(default=None, max_length=500)
    is_pinned: bool | None = None


class HistoryOut(BaseModel):
    """Metadata-only view -- deliberately does NOT include `storage_path`
    (see the comment on that column in app/models/user_history.py) or the
    artifact `payload` itself (fetching every list row's JSON out of
    Supabase Storage just to list history would defeat the point of
    splitting metadata from payload in the first place). Used for
    POST /history/, GET /history/, and PATCH /history/{id}."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    user_id: UUID
    feature_key: str
    title: str
    is_pinned: bool
    created_at: datetime


class HistoryDetail(HistoryOut):
    """GET /history/{id}'s response shape: metadata (Postgres) merged with
    the full artifact payload (fetched from Supabase Storage), per the
    ask's explicit 3-step retrieval flow."""

    payload: dict[str, Any]
