import logging
from datetime import timedelta
from uuid import UUID

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel

from app.api.deps import CurrentUser, DbSession
from app.models.opportunity import Opportunity
from app.services.trend_predictor import TrendPredictionResult, trend_predictor_gateway

logger = logging.getLogger(__name__)

# Shares the "/opportunities" prefix with opportunities.py's router --
# FastAPI supports multiple routers contributing paths under the same
# prefix, and keeping this in its own file (as the ask asked for) while
# still producing the literal requested path
# ("GET /opportunities/{opportunity_id}/predict-trends") only works this
# way, since opportunities.py already owns that prefix.
router = APIRouter(prefix="/opportunities", tags=["trend-predictor"])

# Reused directly from the ask's own example reasoning text
# ("average application window of 65 days") as the assumed window length
# for the synthetic single-cycle estimate below -- see the README for
# the full explanation of why this endpoint has to synthesize rather
# than query real multi-year history.
_ASSUMED_WINDOW_DAYS = 65


class PredictTrendsResponse(BaseModel):
    opportunity_id: UUID
    opportunity_title: str
    data_source: str = "synthetic_estimate"
    data_source_note: str
    result: TrendPredictionResult


@router.get("/{opportunity_id}/predict-trends", response_model=PredictTrendsResponse)
async def predict_trends(
    opportunity_id: UUID, current_user: CurrentUser, db: DbSession
) -> PredictTrendsResponse:
    opportunity = await db.get(Opportunity, opportunity_id)
    if opportunity is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Opportunity not found")

    if opportunity.deadline is None:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="This opportunity has no deadline on record to estimate a cycle pattern from.",
        )

    # No real historical-cycle table exists yet (see the README's Step
    # 3.30 section for the AskUserQuestion gap this came from) -- this
    # synthesizes exactly ONE historical entry from the opportunity's own
    # stored deadline, assuming a typical application window. Because the
    # service treats a single data point as insufficient to establish a
    # real trend (see trend_predictor.py's `_build_facts_block`), this
    # naturally forces `confidence_level` to "Low" -- there's no separate
    # "is this synthetic" flag needed for that part. What the service
    # CANNOT know on its own is that this single point isn't even real
    # historical data at all -- that's disclosed explicitly below via
    # `data_source`/`data_source_note`, never silently presented as fact.
    close_date = opportunity.deadline
    open_date = close_date - timedelta(days=_ASSUMED_WINDOW_DAYS)
    synthetic_historical_dates = [
        {
            "year": close_date.year,
            "open_date": open_date.date().isoformat(),
            "close_date": close_date.date().isoformat(),
        }
    ]

    try:
        result = await trend_predictor_gateway.predict_upcoming_cycle(synthetic_historical_dates)
    except Exception:
        logger.exception(
            "Trend prediction failed for user %s, opportunity %s", current_user.id, opportunity.id
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Scholarship trend prediction is temporarily unavailable.",
        )

    return PredictTrendsResponse(
        opportunity_id=opportunity.id,
        opportunity_title=opportunity.title,
        data_source="synthetic_estimate",
        data_source_note=(
            f"No real multi-year deadline history is tracked for this opportunity yet. "
            f"This prediction is based on a SYNTHETIC single-cycle estimate derived from "
            f"the opportunity's own recorded deadline ({close_date.date().isoformat()}), "
            f"assuming a typical {_ASSUMED_WINDOW_DAYS}-day application window -- not real "
            f"historical fact. Treat this as a rough estimate, not a confirmed trend."
        ),
        result=result,
    )
