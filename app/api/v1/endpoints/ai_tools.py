import logging
from datetime import datetime, timezone
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from sqlalchemy.ext.asyncio import AsyncSession

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.schemas.ai_tools import (
    DocumentGenerationRequest,
    GeneratedDocumentOut,
    RecommendationLetterRequest,
)
from app.services.ai import ai_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai-tools", tags=["ai-tools"])


async def _get_opportunity_or_404(opportunity_id: UUID, db: AsyncSession) -> Opportunity:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")
    return opportunity


def _generation_failed(document_kind: str, user_id, opportunity_id) -> HTTPException:
    logger.exception(
        "%s generation failed for user %s, opportunity %s", document_kind, user_id, opportunity_id
    )
    return HTTPException(
        status_code=status.HTTP_502_BAD_GATEWAY,
        detail="Document generation is temporarily unavailable.",
    )


@router.post("/generate/cv", response_model=GeneratedDocumentOut)
async def generate_cv(
    payload: DocumentGenerationRequest, current_user: CurrentUser, db: DbSession
) -> GeneratedDocumentOut:
    opportunity = await _get_opportunity_or_404(payload.opportunity_id, db)

    try:
        content = await ai_gateway.generate_cv(
            opportunity_title=opportunity.title,
            opportunity_description=opportunity.description,
            eligibility_criteria=opportunity.eligibility_criteria,
            user_background=payload.user_background,
            key_achievements=payload.key_achievements,
            additional_context=payload.additional_context,
        )
    except Exception:
        raise _generation_failed("CV", current_user.id, opportunity.id)

    return GeneratedDocumentOut(
        document_type="cv",
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        content_markdown=content,
        generated_at=datetime.now(timezone.utc),
    )


@router.post("/generate/motivation-letter", response_model=GeneratedDocumentOut)
async def generate_motivation_letter(
    payload: DocumentGenerationRequest, current_user: CurrentUser, db: DbSession
) -> GeneratedDocumentOut:
    opportunity = await _get_opportunity_or_404(payload.opportunity_id, db)

    try:
        content = await ai_gateway.generate_motivation_letter(
            opportunity_title=opportunity.title,
            opportunity_description=opportunity.description,
            eligibility_criteria=opportunity.eligibility_criteria,
            user_background=payload.user_background,
            key_achievements=payload.key_achievements,
            additional_context=payload.additional_context,
        )
    except Exception:
        raise _generation_failed("Motivation letter", current_user.id, opportunity.id)

    return GeneratedDocumentOut(
        document_type="motivation_letter",
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        content_markdown=content,
        generated_at=datetime.now(timezone.utc),
    )


@router.post("/generate/recommendation-letter", response_model=GeneratedDocumentOut)
async def generate_recommendation_letter(
    payload: RecommendationLetterRequest, current_user: CurrentUser, db: DbSession
) -> GeneratedDocumentOut:
    opportunity = await _get_opportunity_or_404(payload.opportunity_id, db)

    try:
        content = await ai_gateway.generate_recommendation_letter(
            opportunity_title=opportunity.title,
            opportunity_description=opportunity.description,
            eligibility_criteria=opportunity.eligibility_criteria,
            user_background=payload.user_background,
            key_achievements=payload.key_achievements,
            additional_context=payload.additional_context,
            recommender_name=payload.recommender_name,
            recommender_role=payload.recommender_role,
        )
    except Exception:
        raise _generation_failed("Recommendation letter", current_user.id, opportunity.id)

    return GeneratedDocumentOut(
        document_type="recommendation_letter",
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        content_markdown=content,
        generated_at=datetime.now(timezone.utc),
    )
