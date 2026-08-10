import asyncio
import logging

from ddgs.exceptions import RatelimitException, TimeoutException
from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from langchain_community.utilities import DuckDuckGoSearchAPIWrapper
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as the other Gemini-backed services in this
# codebase: `errors.ClientError` (429s) and `errors.ServerError` (5xx)
# both inherit from `errors.APIError`. This only covers the Gemini call
# -- the DuckDuckGo call is NOT retried automatically (see
# `WebSearchRateLimitedError` below for why).
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class WebSearchRateLimitedError(RuntimeError):
    """Raised when DuckDuckGo rate-limits the search request
    (`ddgs.exceptions.RatelimitException`). Deliberately NOT retried
    automatically here -- blindly retrying a rate-limited free search
    backend would make the rate-limiting worse, not better. The router
    surfaces this as its own distinct status so the caller can decide
    when to retry."""


class WebSearchTimeoutError(RuntimeError):
    """Raised when the DuckDuckGo search request times out
    (`ddgs.exceptions.TimeoutException`)."""


class LiveOpportunityResult(BaseModel):
    title: str = Field(description="The opportunity's title, as found in the search result.")
    source_url: str = Field(
        description="The exact URL of the search result this was drawn from -- "
        "copied verbatim from the raw result provided, never invented."
    )
    brief_summary: str = Field(
        description="A short, factual summary of what this opportunity is, "
        "based only on the search result's actual title/snippet text."
    )
    deadline_hint: str | None = Field(
        default=None,
        description="A deadline or application-window mention, only if one "
        "is actually present in the result's text -- null if none is "
        "mentioned, never guessed or estimated.",
    )


class WebExplorerResult(BaseModel):
    """Structured output for `search_live_opportunities`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    live_results: list[LiveOpportunityResult] = Field(default_factory=list)


_WEB_EXPLORER_SYSTEM_PROMPT = """\
You are cleaning up and summarizing raw, real-time web search results to \
help a student find scholarships, internships, volunteer programs, or \
courses that may be too new or too niche to be in a curated database yet.

You will be given a numbered list of raw search results, each with its \
title, URL, and a short snippet/description exactly as returned by the \
search engine.

Your job:
1. Review every raw result. Keep only the ones that genuinely appear to be \
a real scholarship, internship, volunteer opportunity, or course listing \
(or a credible page about one) -- discard results that are clearly \
unrelated, spam, or generic informational pages with no actual opportunity \
being described.
2. For each kept result, produce a clean `title`, copy its `source_url` \
EXACTLY as given (character-for-character, from the raw result's URL -- \
never alter, guess, or fabricate a URL), and write a short factual \
`brief_summary` based only on that result's actual title/snippet text.
3. Set `deadline_hint` only if the result's own text actually mentions a \
date, deadline, or application window -- leave it null if none is \
mentioned. Do not estimate or infer a plausible-sounding deadline that \
wasn't actually stated.

Rules:
- Never invent a result that wasn't in the raw list, and never invent or \
modify a `source_url` -- a wrong URL sends a student to the wrong place, \
which is worse than including fewer results.
- If none of the raw results look like real opportunities, return an \
empty `live_results` list rather than forcing a match.
- Do not add commentary, opinions, or recommendations beyond what the \
schema asks for.
"""


def _format_raw_results(raw_results: list[dict]) -> str:
    blocks = []
    for i, r in enumerate(raw_results, start=1):
        blocks.append(
            f"Result {i}:\n"
            f"Title: {r.get('title', '')}\n"
            f"URL: {r.get('link', '')}\n"
            f"Snippet: {r.get('snippet', '')}"
        )
    return "\n\n".join(blocks)


class WebExplorerGateway:
    """Async wrapper around a live DuckDuckGo search (via
    `langchain_community`'s `DuckDuckGoSearchAPIWrapper`) followed by
    Gemini-based cleanup/summarization -- same
    `google-genai`-fresh-client-per-call posture as every other
    Gemini-backed service here (gemini-1.5-flash / google-generativeai
    substitution rationale from Step 3.4 applies again).

    `DuckDuckGoSearchAPIWrapper` (structured `.results()`, a list of
    {title, link, snippet} dicts) was chosen over `DuckDuckGoSearchRun`
    (a single concatenated string blob) specifically because
    `source_url` needs to be extracted reliably per-result -- asking
    Gemini to pull a URL back out of one unstructured text blob is a much
    higher hallucination/mis-attribution risk than handing it a clean,
    pre-separated URL per result to copy verbatim.
    """

    def _search_wrapper(self) -> DuckDuckGoSearchAPIWrapper:
        return DuckDuckGoSearchAPIWrapper(max_results=settings.web_explorer_max_results)

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    async def run_search(self, query: str) -> list[dict]:
        """Runs the blocking `ddgs`-backed search in a worker thread --
        same `asyncio.to_thread` pattern used for `youtube_transcript_api`
        in Step 3.19, since neither library has a native async API.

        Public (not underscore-prefixed) as of Step 3.27 -- the
        Autonomous Scholar Agent's Celery task
        (`app/workers/scholar_agent.py`) needs this exact same
        DDG-search-with-rate-limit/timeout-mapping logic, and reusing it
        here avoids a second copy of the same exception handling. Same
        "rename for genuine cross-module reuse" precedent already used
        for `autofill_agent.py`'s `format_profile_text` in Step 3.20.
        """
        wrapper = self._search_wrapper()
        try:
            return await asyncio.to_thread(
                wrapper.results, query, max_results=settings.web_explorer_max_results
            )
        except RatelimitException as exc:
            raise WebSearchRateLimitedError(
                "DuckDuckGo search is currently rate-limiting requests. "
                "Please try again shortly."
            ) from exc
        except TimeoutException as exc:
            raise WebSearchTimeoutError("The live web search timed out.") from exc

    @_retry_gemini_call
    async def _summarize(self, query: str, raw_results: list[dict]) -> WebExplorerResult:
        client = self._client()

        prompt = (
            f"Search query: {query}\n\n"
            f"Raw search results:\n{_format_raw_results(raw_results)}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_web_explorer_model,
            contents=[_WEB_EXPLORER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=WebExplorerResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for live web result summarization")

        return WebExplorerResult.model_validate_json(response.text)

    async def search_live_opportunities(self, query: str) -> WebExplorerResult:
        """Run `query` through a live DuckDuckGo search, then have Gemini
        clean up, verify (as far as verifiable from the given text), and
        summarize the results into `live_results`.

        Raises `WebSearchRateLimitedError` or `WebSearchTimeoutError` if
        the underlying search fails for those specific reasons.
        """
        raw_results = await self.run_search(query)

        if not raw_results:
            # Nothing to summarize -- skip the Gemini call entirely
            # rather than spending a call on an empty prompt.
            return WebExplorerResult(live_results=[])

        return await self._summarize(query, raw_results)


web_explorer_gateway = WebExplorerGateway()
