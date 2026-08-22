from datetime import datetime
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field

from app.models.enums import AcademicLevel, Gender, OpportunityType, SourceType

T = TypeVar("T")


class SourceOut(BaseModel):
    """Minimal Source info nested inside OpportunityOut. Cheap to include:
    Opportunity.source is eager-loaded via lazy="selectin" (see
    app/models/opportunity.py), so this never triggers an extra query.

    `source_type` addition -- Hybrid Opportunity Aggregation Engine step.
    This is where that step's own "source_type" concept is actually
    exposed to API consumers (nested under `OpportunityOut.source`, i.e.
    `opportunity.source.source_type`) -- see `app/models/source.py`'s own
    module docstring for why it's not a second, duplicate field directly
    on `OpportunityOut` itself.
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    source_type: SourceType


class OpportunityOut(BaseModel):
    """Public API shape for an Opportunity.

    Deliberately excludes `raw_extracted_data` (internal scrape payload --
    not meant for public consumption, and can contain arbitrary scraped
    HTML/text) and `embedding` (a 1536-float vector with no client-side
    use, that would needlessly bloat every list response).
    """

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    title: str
    description: str | None
    target_url: str
    type: OpportunityType
    deadline: datetime | None
    eligibility_criteria: str | None
    funding_details: str | None
    location: str | None
    country: str | None
    is_verified: bool
    ai_confidence_score: float | None
    # Hybrid Opportunity Aggregation Engine additions.
    is_local_egypt: bool
    target_age_group: str | None
    # Backend Folder Structure & Core Database Models step addition --
    # see app/models/opportunity.py's own comment on this column.
    # DISCLOSED SCOPE BOUNDARY: exposed here for reads, but NOT added to
    # `OpportunityCreate` below -- `aggregator_service.py`'s own Gemini
    # extraction prompt has not been taught to populate this field (out
    # of scope for this pass; see this file's own module-level judgment
    # call in the README). Every row this schema currently serializes
    # will have `target_academic_levels: null` until either a future
    # aggregator-prompt update or manual admin curation populates it.
    target_academic_levels: list[AcademicLevel] | None
    # Real Age/Academic-Year/Gender Opportunity Filtering step additions.
    # Same disclosed scope boundary as `target_academic_levels` directly
    # above: exposed here for reads, NOT added to `OpportunityCreate`
    # below -- the real Gemini aggregator pipeline has not been taught to
    # extract a numeric age range or a gender restriction from a scraped
    # posting (confirmed via a direct read of that prompt/schema). Every
    # existing row will have these as `null` (unrestricted) until a
    # future aggregator-prompt update or manual admin curation populates
    # them.
    min_age: int | None
    max_age: int | None
    target_gender: Gender | None
    source: SourceOut
    created_at: datetime
    updated_at: datetime


class OpportunitySearchResult(OpportunityOut):
    """OpportunityOut plus the similarity score from a semantic search hit."""

    similarity_score: float = Field(
        description="Cosine similarity to the query embedding, roughly in "
        "[0, 1] for typical text embeddings (1 = identical direction). "
        "Higher is more relevant."
    )


class PaginatedResponse(BaseModel, Generic[T]):
    items: list[T]
    total: int
    limit: int
    offset: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def has_more(self) -> bool:
        return self.offset + len(self.items) < self.total


class OpportunitySearchRequest(BaseModel):
    query: str = Field(
        min_length=1,
        max_length=500,
        description="Natural-language search query, e.g. 'Medical scholarships in Europe'.",
    )
    limit: int = Field(default=10, ge=1, le=50)
    min_similarity: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Override the server-side default similarity threshold "
        "(app.core.config.settings.search_similarity_threshold) for this "
        "request only.",
    )


class OpportunityCreate(BaseModel):
    """Hybrid Opportunity Aggregation Engine step's own explicitly-named
    output schema: `process_raw_post_via_ai` (app/services/aggregator_service.py)
    parses the AI's raw JSON output and maps it into exactly this shape,
    which `aggregator_service.upsert_opportunity` then writes into an
    `Opportunity` row (`type=self.opportunity_type`, `target_url=
    self.target_url`, ...).

    Field-naming notes, consistent with the disclosed reasoning in
    `app/models/opportunity.py`'s own module docstring:
    - `target_url` here IS this step's "original_link" -- the ask's own
      name for a concept this codebase already calls `target_url`.
    - `source_name`/`source_type` are carried on THIS schema (unlike the
      ORM model, where they're deliberately NOT duplicated columns) --
      here they're just plain input data the caller already has from the
      sources config, needed to resolve/create the right `Source` row.
      They never get written as columns on `Opportunity` itself.
    - `opportunity_type` (`OpportunityType`) is a disclosed, NECESSARY
      addition beyond this step's own literal field list: `Opportunity.type`
      is a real `NOT NULL` column with no default (see
      app/models/opportunity.py) that predates this step and has nothing
      to do with local/global classification -- a value MUST be supplied
      for every insert, so `process_raw_post_via_ai`'s own Gemini prompt
      additionally asks for a best-fit classification into the four
      pre-existing categories (scholarship/internship/volunteer/course),
      with a disclosed `COURSE` fallback if Gemini's answer doesn't map
      cleanly onto any of them (see that function's own docstring).
    """

    title: str = Field(min_length=1, max_length=500)
    description: str | None = None
    target_url: str = Field(min_length=1, max_length=2048, description="This step's own 'original_link'.")
    opportunity_type: OpportunityType
    deadline: datetime | None = None
    country: str = Field(default="Global")
    is_local_egypt: bool
    target_age_group: str | None = None
    source_name: str = Field(min_length=1, max_length=255)
    source_type: SourceType
    source_base_url: str = Field(
        min_length=1,
        max_length=2048,
        description="Disclosed addition beyond this step's own literal field list -- "
        "`Source.base_url` is a real NOT NULL column (app/models/source.py); needed "
        "so `upsert_opportunity` can get-or-create the right `Source` row.",
    )
