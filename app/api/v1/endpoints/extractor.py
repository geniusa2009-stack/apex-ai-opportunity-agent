import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field, model_validator

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.extractor import ExtractedDeadline, extractor_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract-deadline", tags=["extractor"])


class ExtractDeadlineRequest(BaseModel):
    raw_text: str | None = Field(
        default=None,
        min_length=10,
        max_length=8000,
        description="Raw text of an opportunity to scan for a deadline.",
    )
    opportunity_id: UUID | None = Field(
        default=None,
        description="Alternatively, extract from an already-stored "
        "opportunity's title/description/eligibility/funding text.",
    )

    # Same "exactly one of raw_text/opportunity_id" validation as
    # Feature 9's CheckDuplicateRequest -- this router accepts the same
    # two input shapes for the same reason (either a not-yet-stored
    # listing's raw text, or an existing row).
    @model_validator(mode="after")
    def _exactly_one_source(self) -> "ExtractDeadlineRequest":
        if bool(self.raw_text) == bool(self.opportunity_id):
            raise ValueError("Provide exactly one of raw_text or opportunity_id, not both/neither.")
        return self


def _build_opportunity_text(opportunity: Opportunity) -> str:
    # Deliberately excludes the already-structured `deadline` column --
    # the point of this feature is to extract a deadline out of raw,
    # unstructured text (description/eligibility/funding details), not
    # to echo back a date that's already been cleanly parsed and stored.
    lines = [f"Title: {opportunity.title}"]
    if opportunity.description:
        lines.append(f"Description: {opportunity.description}")
    if opportunity.eligibility_criteria:
        lines.append(f"Eligibility criteria: {opportunity.eligibility_criteria}")
    if opportunity.funding_details:
        lines.append(f"Funding details: {opportunity.funding_details}")
    return "\n".join(lines)


# Path is exactly "/extract-deadline" -- same `@router.post("")` +
# `prefix="/extract-deadline"` pattern used by every other single-purpose
# POST endpoint since Feature 1.
@router.post("", response_model=ExtractedDeadline)
async def extract_deadline(
    payload: ExtractDeadlineRequest, current_user: CurrentUser, db: DbSession
) -> ExtractedDeadline:
    if payload.opportunity_id is not None:
        opportunity = await db.get(Opportunity, payload.opportunity_id)
        if opportunity is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
        opportunity_text = _build_opportunity_text(opportunity)
    else:
        opportunity_text = payload.raw_text

    try:
        return await extractor_gateway.extract_deadline(opportunity_text)
    except Exception:
        logger.exception("Deadline extraction failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Deadline extraction is temporarily unavailable.",
        )
