import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.portfolio_generator import PortfolioResult, portfolio_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/generate-portfolio", tags=["portfolio"])


class GeneratePortfolioRequest(BaseModel):
    student_profile_text: str = Field(
        min_length=20,
        max_length=8000,
        description="The student's profile: background, education, "
        "skills, projects/achievements, in free text.",
    )
    theme_color: str = Field(
        default="blue",
        min_length=2,
        max_length=30,
        description="Accent color for the generated page, as a Tailwind "
        "color name (e.g. 'blue', 'emerald', 'rose').",
    )


class GeneratePortfolioResponse(BaseModel):
    result: PortfolioResult


# Path is exactly "/generate-portfolio" -- same `@router.post("")` +
# `prefix="/generate-portfolio"` pattern used by every other
# single-purpose POST endpoint since Feature 1.
@router.post("", response_model=GeneratePortfolioResponse)
async def generate_portfolio(
    payload: GeneratePortfolioRequest, current_user: CurrentUser
) -> GeneratePortfolioResponse:
    # No opportunity/DB lookup here -- same precedent as polisher.py
    # (Feature 7) and professor.py (Advanced Feature 1): this operates
    # purely on the profile text and theme color the caller supplies.
    try:
        result = await portfolio_gateway.generate_portfolio_html(
            payload.student_profile_text, payload.theme_color
        )
    except Exception:
        logger.exception("Portfolio generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Portfolio generation is temporarily unavailable.",
        )

    return GeneratePortfolioResponse(result=result)
