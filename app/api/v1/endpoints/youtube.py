import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.youtube_extractor import (
    InvalidYouTubeUrlError,
    VideoInsights,
    YouTubeTranscriptUnavailableError,
    youtube_insights_gateway,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/extract-youtube-insights", tags=["youtube"])


class ExtractYouTubeInsightsRequest(BaseModel):
    youtube_url: str = Field(
        min_length=1,
        max_length=500,
        description="A YouTube video URL (youtube.com/watch, youtu.be, "
        "shorts, embed) or a bare 11-character video ID.",
    )


class ExtractYouTubeInsightsResponse(BaseModel):
    result: VideoInsights


# Path is exactly "/extract-youtube-insights" -- same `@router.post("")`
# + `prefix="/extract-youtube-insights"` pattern used by every other
# single-purpose POST endpoint since Feature 1.
@router.post("", response_model=ExtractYouTubeInsightsResponse)
async def extract_youtube_insights(
    payload: ExtractYouTubeInsightsRequest, current_user: CurrentUser
) -> ExtractYouTubeInsightsResponse:
    try:
        result = await youtube_insights_gateway.extract_video_insights(payload.youtube_url)
    except InvalidYouTubeUrlError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc))
    except YouTubeTranscriptUnavailableError as exc:
        # This is a property of the specific video (captions disabled,
        # none in a usable language, video unavailable/private/age-
        # restricted) -- a 422 tells the caller the request itself was
        # well-formed but this particular video can't be processed,
        # distinct from a 502 (our AI gateway failing).
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"This video has no available transcript. {exc}",
        )
    except Exception:
        logger.exception("YouTube insight extraction failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="YouTube insight extraction is temporarily unavailable.",
        )

    return ExtractYouTubeInsightsResponse(result=result)
