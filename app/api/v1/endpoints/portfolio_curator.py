import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.portfolio_curator import (
    PortfolioCuratorResult,
    portfolio_curator_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /portfolio-curator/plan") --
# `prefix` carries the first segment, `@router.post("/plan")` the
# second, same multi-segment approach as `/uwc-strategist/plan`.
#
# Distinct prefix/tag from portfolio.py's existing "/portfolio" router
# (Advanced Feature 3, Step 3.17, portfolio WEBSITE generation) -- this
# is portfolio CONTENT curation, a different feature entirely, so no
# path collision.
router = APIRouter(prefix="/portfolio-curator", tags=["portfolio-curator"])


class CuratePortfolioRequest(BaseModel):
    art_medium: str = Field(
        min_length=1, max_length=255, description='E.g. "Charcoal Drawing", "Digital 3D", "Photography".'
    )
    artwork_themes: list[str] = Field(min_length=1)


class CuratePortfolioResponse(BaseModel):
    result: PortfolioCuratorResult


# Path is exactly "/portfolio-curator/plan" -- `prefix="/portfolio-curator"`
# + `@router.post("/plan")`.
@router.post("/plan", response_model=CuratePortfolioResponse)
async def curate_portfolio(
    payload: CuratePortfolioRequest, current_user: CurrentUser
) -> CuratePortfolioResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await portfolio_curator_gateway.curate_portfolio(
            payload.art_medium,
            payload.artwork_themes,
        )
    except Exception:
        logger.exception("Portfolio curation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Portfolio curation is temporarily unavailable.",
        )

    return CuratePortfolioResponse(result=result)
