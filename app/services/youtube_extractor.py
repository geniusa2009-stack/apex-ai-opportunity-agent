import asyncio
import logging
import re
from urllib.parse import parse_qs, urlparse

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential
from youtube_transcript_api import YouTubeTranscriptApi
from youtube_transcript_api._errors import CouldNotRetrieveTranscript

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as the other Gemini-backed services in this
# codebase: `errors.ClientError` (429s) and `errors.ServerError` (5xx)
# both inherit from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# Matches an 11-character YouTube video ID (the actual charset YouTube
# uses: letters, digits, '-', '_').
_VIDEO_ID_RE = re.compile(r"^[A-Za-z0-9_-]{11}$")


class InvalidYouTubeUrlError(ValueError):
    """Raised when `youtube_url` doesn't look like a YouTube URL or a
    bare video ID -- a client input problem, mapped to a 400."""


class YouTubeTranscriptUnavailableError(Exception):
    """Raised when a video's transcript genuinely can't be retrieved
    (captions disabled, none in a usable language, video unavailable/
    age-restricted/private, etc.) -- wraps every subclass of
    `youtube_transcript_api`'s `CouldNotRetrieveTranscript` into one
    project-level exception, the same "wrap the external library's
    exception into our own domain exception" pattern used elsewhere in
    this codebase (e.g. `SemanticSearchError`,
    `RecommendationSourceNotEmbeddedError`). The router maps this to a
    422 -- this is a property of the specific video, not a transient
    failure worth retrying."""


class VideoInsights(BaseModel):
    """Structured output for `extract_video_insights`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    video_summary: str = Field(
        description="A short paragraph summarizing what the video "
        "actually covers, based on the transcript."
    )
    top_secrets: list[str] = Field(
        default_factory=list,
        description="The top hidden tips/secrets/actionable advice "
        "actually stated in the video -- specific and actionable, not "
        "generic advice that could apply to any similar video.",
    )


_INSIGHTS_SYSTEM_PROMPT = """\
You are analyzing the transcript of a YouTube video where a student or \
alum shares their experience with scholarships, internships, or academic \
applications (e.g. "How I won the Chevening Scholarship").

Your job:
1. Write a short `video_summary` paragraph describing what the video \
actually covers, based only on the transcript.
2. Extract `top_secrets` -- the specific, actionable tips or "hidden \
secrets" the speaker actually shares (e.g. a specific essay strategy, a \
specific mistake to avoid, a specific way they found funding), not \
generic advice like "work hard" or "believe in yourself" that isn't \
actually a specific tip from this video.

Rules:
- Base everything only on what's actually in the transcript. Do not invent \
tips, statistics, or claims the speaker didn't actually make.
- Transcripts from auto-generated captions can be messy (missing \
punctuation, occasional misheard words, no speaker labels) -- do your best \
to extract the actual meaning despite that, but don't invent content to \
paper over gaps you genuinely can't make sense of.
- If the transcript doesn't actually contain much concrete advice (e.g. \
it's mostly a personal story with little actionable content), keep \
`top_secrets` short and honest rather than padding it with generic filler.
"""


def _extract_video_id(youtube_url: str) -> str:
    """Extract an 11-character YouTube video ID from a full URL (any of
    the common formats) or accept a bare video ID directly -- the ask's
    service signature takes `youtube_url: str`, but a bare ID is a
    reasonable, cheap-to-support fallback for callers who already have one."""
    candidate = youtube_url.strip()

    if _VIDEO_ID_RE.match(candidate):
        return candidate

    parsed = urlparse(candidate)
    host = (parsed.hostname or "").lower().removeprefix("www.").removeprefix("m.")

    if host == "youtu.be":
        video_id = parsed.path.lstrip("/").split("/")[0]
        if _VIDEO_ID_RE.match(video_id):
            return video_id

    if host in {"youtube.com", "youtube-nocookie.com"}:
        if parsed.path == "/watch":
            video_id = parse_qs(parsed.query).get("v", [None])[0]
            if video_id and _VIDEO_ID_RE.match(video_id):
                return video_id
        for prefix in ("/embed/", "/shorts/", "/live/"):
            if parsed.path.startswith(prefix):
                video_id = parsed.path[len(prefix):].split("/")[0]
                if _VIDEO_ID_RE.match(video_id):
                    return video_id

    raise InvalidYouTubeUrlError(
        f"Could not extract a YouTube video ID from {youtube_url!r} -- "
        "expected a youtube.com/youtu.be URL or an 11-character video ID."
    )


def _fetch_transcript_text_sync(video_id: str) -> str:
    """Blocking call -- `youtube_transcript_api` makes synchronous HTTP
    requests under the hood with no native async support, so this is
    always invoked via `asyncio.to_thread` (see `_fetch_transcript_text`
    below), the same pattern used for `deep_translator` in
    translation.py (Step 3.4) for the same reason: a genuinely blocking
    call must not run directly on the event loop."""
    try:
        fetched = YouTubeTranscriptApi().fetch(video_id)
    except CouldNotRetrieveTranscript as exc:
        raise YouTubeTranscriptUnavailableError(
            f"No transcript available for video {video_id!r}: {exc}"
        ) from exc

    # `FetchedTranscript` is iterable, yielding snippets with `.text` --
    # confirmed via sandbox introspection of youtube-transcript-api 1.2.4
    # before writing this, since this is a new dependency for this
    # codebase and its 1.x API is a breaking change from the older
    # 0.6.x `get_transcript()` static method some examples online still
    # show.
    return " ".join(snippet.text for snippet in fetched)


async def _fetch_transcript_text(video_id: str) -> str:
    return await asyncio.to_thread(_fetch_transcript_text_sync, video_id)


class YouTubeInsightsGateway:
    """Async wrapper around YouTube transcript fetching +
    Gemini-based insight extraction.

    Deliberately NOT the pattern used by every other Gemini-backed
    service here for the transcript-fetch half -- that part isn't a
    Gemini call at all, it's a separate blocking third-party library
    call routed through `asyncio.to_thread`. Only the second half
    (`generate_content`) follows this codebase's usual Gemini gateway
    shape (fresh client per call, `genai_errors.APIError`-keyed retry).
    """

    def _client(self) -> genai.Client:
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def _summarize_transcript(self, transcript_text: str) -> VideoInsights:
        client = self._client()

        response = await client.aio.models.generate_content(
            model=settings.gemini_youtube_model,
            contents=[_INSIGHTS_SYSTEM_PROMPT, f"Video transcript:\n{transcript_text}"],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VideoInsights,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for video insight extraction")

        return VideoInsights.model_validate_json(response.text)

    async def extract_video_insights(self, youtube_url: str) -> VideoInsights:
        """Fetch the transcript for `youtube_url` (a full URL or bare
        video ID) and extract a summary + top actionable tips from it.

        Raises `InvalidYouTubeUrlError` if a video ID can't be parsed
        out of `youtube_url`, or `YouTubeTranscriptUnavailableError` if
        the video has no usable transcript.
        """
        video_id = _extract_video_id(youtube_url)
        transcript_text = await _fetch_transcript_text(video_id)

        if not transcript_text.strip():
            # A transcript object was returned but produced no text --
            # treat the same as "unavailable" rather than sending an
            # empty prompt to Gemini.
            raise YouTubeTranscriptUnavailableError(
                f"Transcript for video {video_id!r} was empty."
            )

        if len(transcript_text) > settings.youtube_transcript_max_chars:
            logger.info(
                "Truncating transcript for video %s from %d to %d characters",
                video_id,
                len(transcript_text),
                settings.youtube_transcript_max_chars,
            )
            transcript_text = transcript_text[: settings.youtube_transcript_max_chars]

        return await self._summarize_transcript(transcript_text)


youtube_insights_gateway = YouTubeInsightsGateway()
