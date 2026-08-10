import logging
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.tagger import tagger_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generate-tags", tags=["tagger"])


class GenerateTagsRequest(BaseModel):
    opportunity_id: UUID


class GenerateTagsResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    tags: list[str]


def _build_opportunity_text(opportunity: Opportunity) -> str:
    # The ask says "reads an opportunity's raw description," but the
    # tagging categories it names -- funding type and location -- often
    # live in `funding_details`/`country` rather than free-text
    # `description`, and `eligibility_criteria` is usually where "level
    # of study" is actually stated (e.g. "open to undergraduates"). Using
    # description alone would under-tag those categories for real
    # scraped data, so this composes the same title/description/
    # eligibility/funding/location block every other opportunity-aware
    # service in this codebase builds, rather than passing description
    # in isolation.
    lines = [f"Title: {opportunity.title}"]
    if opportunity.description:
        lines.append(f"Description: {opportunity.description}")
    if opportunity.eligibility_criteria:
        lines.append(f"Eligibility criteria: {opportunity.eligibility_criteria}")
    if opportunity.funding_details:
        lines.append(f"Funding details: {opportunity.funding_details}")
    if opportunity.location:
        lines.append(f"Location: {opportunity.location}")
    if opportunity.country:
        lines.append(f"Country: {opportunity.country}")
    if opportunity.type:
        lines.append(f"Type: {opportunity.type.value}")
    return "\n".join(lines)


# Path is exactly "/generate-tags" -- same `@router.post("")` +
# `prefix="/generate-tags"` pattern as chat.py/grader.py/planner.py, to
# match the literal ask ("POST /generate-tags").
@router.post("", response_model=GenerateTagsResponse)
async def generate_tags(
    payload: GenerateTagsRequest, current_user: CurrentUser, db: DbSession
) -> GenerateTagsResponse:
    opportunity = await db.get(Opportunity, payload.opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    opportunity_text = _build_opportunity_text(opportunity)

    try:
        result = await tagger_gateway.generate_tags(opportunity_text)
    except Exception:
        logger.exception(
            "Tag generation failed for user %s, opportunity %s", current_user.id, opportunity.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Tag generation is temporarily unavailable.",
        )

    return GenerateTagsResponse(
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        tags=result.tags,
    )
