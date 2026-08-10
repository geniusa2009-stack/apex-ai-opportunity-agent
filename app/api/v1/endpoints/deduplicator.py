import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.ai import ai_gateway
from app.services.deduplicator import DuplicateMatch, find_duplicates

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/check-duplicate", tags=["deduplicator"])


class CheckDuplicateRequest(BaseModel):
    raw_text: str | None = Field(
        default=None,
        min_length=10,
        max_length=8000,
        description="Raw text of a new, not-yet-stored opportunity to "
        "check -- will be embedded using the existing embedding utility.",
    )
    opportunity_id: UUID | None = Field(
        default=None,
        description="Alternatively, check an existing opportunity's "
        "already-stored embedding against the rest of the table.",
    )
    similarity_threshold: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Override the default 0.95 similarity threshold for "
        "this request only.",
    )

    @model_validator(mode="after")
    def _exactly_one_source(self) -> "CheckDuplicateRequest":
        if bool(self.raw_text) == bool(self.opportunity_id):
            raise ValueError("Provide exactly one of raw_text or opportunity_id, not both/neither.")
        return self


class CheckDuplicateResponse(BaseModel):
    is_duplicate: bool
    potential_matches: list[DuplicateMatch]


_DEFAULT_SIMILARITY_THRESHOLD = 0.95


# Path is exactly "/check-duplicate" -- same `@router.post("")` +
# `prefix="/check-duplicate"` pattern used by every other single-purpose
# POST endpoint added since Feature 1, to match the literal ask.
@router.post("", response_model=CheckDuplicateResponse)
async def check_duplicate(
    payload: CheckDuplicateRequest, current_user: CurrentUser, db: DbSession
) -> CheckDuplicateResponse:
    threshold = (
        payload.similarity_threshold
        if payload.similarity_threshold is not None
        else _DEFAULT_SIMILARITY_THRESHOLD
    )
    exclude_id: UUID | None = None

    if payload.opportunity_id is not None:
        opportunity = await db.get(Opportunity, payload.opportunity_id)
        if opportunity is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
        if opportunity.embedding is None:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This opportunity has no embedding yet, so duplicate "
                "checking can't be run for it.",
            )
        embedding = opportunity.embedding
        # Exclude the row itself -- otherwise it always matches itself at
        # similarity 1.0 and would always report as a duplicate of itself.
        exclude_id = payload.opportunity_id
    else:
        try:
            embedding = await ai_gateway.generate_embedding(payload.raw_text)
        except Exception:
            logger.exception("Embedding generation failed for duplicate check, user %s", current_user.id)
            raise HTTPException(
                status_code=status.HTTP_502_BAD_GATEWAY,
                detail="Duplicate checking is temporarily unavailable -- embedding generation failed.",
            )

    matches = await find_duplicates(
        db, embedding, threshold, exclude_opportunity_id=exclude_id
    )

    return CheckDuplicateResponse(is_duplicate=len(matches) > 0, potential_matches=matches)
