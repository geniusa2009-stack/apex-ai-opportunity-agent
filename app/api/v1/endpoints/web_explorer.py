import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.web_explorer import (
    WebExplorerResult,
    WebSearchRateLimitedError,
    WebSearchTimeoutError,
    web_explorer_gateway,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/explore-web", tags=["web-explorer"])


class ExploreWebRequest(BaseModel):
    search_query: str = Field(
        min_length=3,
        max_length=500,
        description="E.g. 'fully funded undergraduate scholarships in Japan 2026'.",
    )


class ExploreWebResponse(BaseModel):
    result: WebExplorerResult


# Path is exactly "/explore-web" -- same `@router.post("")` +
# `prefix="/explore-web"` pattern used by every other single-purpose
# POST endpoint since Feature 1.
@router.post("", response_model=ExploreWebResponse)
async def explore_web(
    payload: ExploreWebRequest, current_user: CurrentUser
) -> ExploreWebResponse:
    # No opportunity/DB lookup here -- same precedent as polisher.py
    # (Feature 7), professor.py (Advanced Feature 1), and
    # devils_advocate.py (Advanced Feature 7): this operates purely on
    # the search query text the caller supplies.
    try:
        result = await web_explorer_gateway.search_live_opportunities(payload.search_query)
    except WebSearchRateLimitedError as exc:
        # A distinct, more specific status than a generic 502 -- the
        # caller can reasonably back off and retry a 429, whereas a 502
        # implies something is more broadly broken.
        raise HTTPException(status_code=status.HTTP_429_TOO_MANY_REQUESTS, detail=str(exc))
    except WebSearchTimeoutError as exc:
        raise HTTPException(status_code=status.HTTP_504_GATEWAY_TIMEOUT, detail=str(exc))
    except Exception:
        logger.exception("Live web opportunity search failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Live web opportunity search is temporarily unavailable.",
        )

    return ExploreWebResponse(result=result)
