import asyncio
import logging
import uuid

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.celery_app import celery_app
from app.core.config import settings
from app.db.session import AsyncSessionLocal, engine
from app.models.agent_alert import AgentAlert
from app.models.user import User
from app.services.web_explorer import (
    LiveOpportunityResult,
    WebSearchRateLimitedError,
    WebSearchTimeoutError,
    web_explorer_gateway,
)

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


class ScholarAgentReport(BaseModel):
    """Structured output for the Gemini profile-matching/report-drafting
    call. Reuses `web_explorer.LiveOpportunityResult` for each match's
    shape (title/source_url/brief_summary/deadline_hint) -- identical
    semantics to Advanced Feature 10's live-search results, so this
    avoids a near-duplicate schema for the same concept.

    Distinct from `WebExplorerResult` (Advanced Feature 10) in that this
    call ALSO filters for profile fit (not just "does this look like a
    real opportunity") and produces a personalized narrative
    `report_text`, not just a bare list.
    """

    matches: list[LiveOpportunityResult] = Field(
        default_factory=list,
        description="Only results that both look like genuine "
        "opportunities AND plausibly fit the student's stated profile.",
    )
    report_text: str = Field(
        description="A short, personalized narrative report addressed to "
        "the student, summarizing what was found and why it's relevant "
        "to their specific profile. Empty-string-appropriate content "
        "(e.g. 'No strong matches were found this time') if `matches` is "
        "empty."
    )


_SCHOLAR_AGENT_SYSTEM_PROMPT = """\
You are an autonomous research assistant that periodically searches the web \
on a student's behalf for scholarships, internships, volunteer programs, or \
courses that fit their specific profile, and drafts a personalized alert \
report summarizing what you found.

You will be given the student's profile (whatever fields are on record -- \
some may be missing) and a numbered list of raw web search results, each \
with its title, URL, and a short snippet/description exactly as returned \
by the search engine.

Your job:
1. Review every raw result. Keep only the ones that BOTH genuinely appear \
to be a real scholarship/internship/volunteer/course listing (not spam or \
an unrelated page) AND plausibly fit the student's stated profile -- e.g. \
don't keep a graduate-only program for an undergraduate student, or a \
program in a country that doesn't match their stated target country, \
unless the result's own text suggests it's actually a fit despite that.
2. For each kept result, produce a clean `title`, copy its `source_url` \
EXACTLY as given (character-for-character, never altered or invented), and \
write a short factual `brief_summary`. Set `deadline_hint` only if the \
result's own text actually mentions one -- never estimate.
3. Write a short, personalized `report_text` addressed directly to the \
student (e.g. "Based on your profile in Computer Science targeting \
Germany, I found..."), summarizing what was found and specifically why \
each match (or the overall search) is relevant to THEIR profile -- not a \
generic summary that could apply to any student.

Rules:
- If none of the raw results are both real and a good fit, return an \
empty `matches` list and write a brief, honest `report_text` explaining \
that no strong matches were found this time (do not force a weak match \
just to have something to report).
- Never invent a result, a `source_url`, or a profile fact that wasn't \
actually given.
- Do not fabricate why something is a "match" -- the fit reasoning in \
`report_text` must be traceable to the student's actual stated profile \
fields and the result's actual text.
"""


def _format_profile(profile: dict) -> str:
    lines = []
    for label, key in (
        ("Major", "major"),
        ("Degree level", "degree_level"),
        ("GPA", "gpa"),
        ("Target country", "target_country"),
    ):
        value = profile.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else "(No profile details provided.)"


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


def _build_search_query(profile: dict) -> str:
    """Constructs a search query from whatever profile fields are
    actually set -- omits any that are missing rather than injecting a
    literal "None" into the query text."""
    parts = ["scholarships"]
    if profile.get("degree_level"):
        parts.append(f"for {profile['degree_level']} students")
    if profile.get("major"):
        parts.append(f"in {profile['major']}")
    if profile.get("target_country"):
        parts.append(f"in {profile['target_country']}")
    return " ".join(parts)


@_retry_gemini_call
async def _draft_report(profile: dict, raw_results: list[dict]) -> ScholarAgentReport:
    client = genai.Client(api_key=settings.gemini_api_key)

    prompt = (
        f"Student profile:\n{_format_profile(profile)}\n\n"
        f"Raw search results:\n{_format_raw_results(raw_results)}"
    )

    response = await client.aio.models.generate_content(
        model=settings.gemini_scholar_agent_model,
        contents=[_SCHOLAR_AGENT_SYSTEM_PROMPT, prompt],
        config=genai_types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ScholarAgentReport,
        ),
    )

    if not response.text:
        raise ValueError("Gemini returned no content for scholar agent report drafting")

    return ScholarAgentReport.model_validate_json(response.text)


@celery_app.task(name="app.workers.scholar_agent.run_autonomous_scholar_search")
def run_autonomous_scholar_search(user_id: str) -> dict:
    """Celery entrypoint (sync). Same `asyncio.run()` + `engine.dispose()`
    pattern as `app.workers.tasks.verify_new_opportunities` -- see that
    task's docstring for the full event-loop-lifetime reasoning (the
    shared async engine binds its pooled connections to whichever loop
    first uses them; disposing the pool in `finally` forces fresh
    connections on the next task run rather than reusing connections tied
    to a now-dead loop).

    `user_id` is a `str`, not an `int` -- the ask said `int`, but every id
    in this schema (including `User.id`) is a UUID, matching the same
    UUID-not-int precedent already disclosed for `recommendations.py`
    (Advanced Feature 8) and `alumni_matchmaker.py` (Advanced Feature 8
    of the Advanced tier). `str` specifically (not passing a `UUID`
    object directly) because Celery's configured `task_serializer="json"`
    (see `app/core/celery_app.py`) can't serialize a raw `UUID` -- the
    task converts it back with `uuid.UUID(user_id)` internally.
    """
    return asyncio.run(_run_autonomous_scholar_search(user_id))


async def _run_autonomous_scholar_search(user_id: str) -> dict:
    try:
        async with AsyncSessionLocal() as session:
            user = await session.get(User, uuid.UUID(user_id))
            if user is None:
                logger.warning("Scholar agent: user %s not found", user_id)
                return {"status": "user_not_found", "user_id": user_id}

            profile = {
                "major": user.major,
                "degree_level": user.degree_level,
                "gpa": user.gpa,
                "target_country": user.target_country,
            }
            if not any(profile.values()):
                # Same posture as autofill.py/probability.py's
                # all-empty-profile checks -- nothing to search for. The
                # router (see scholar_agent.py's endpoint) already
                # guards against dispatching this task with an empty
                # profile, but the task re-checks independently since it
                # can also be triggered by anything else that calls
                # `.delay()` directly, bypassing the router's check.
                logger.info("Scholar agent: user %s has no profile data set", user_id)
                return {"status": "no_profile_data", "user_id": user_id}

            query = _build_search_query(profile)

            try:
                raw_results = await web_explorer_gateway.run_search(query)
            except (WebSearchRateLimitedError, WebSearchTimeoutError) as exc:
                logger.warning("Scholar agent: search failed for user %s: %s", user_id, exc)
                return {"status": "search_failed", "user_id": user_id, "reason": str(exc)}

            if not raw_results:
                logger.info("Scholar agent: no raw search results for user %s", user_id)
                return {"status": "no_results", "user_id": user_id, "query": query}

            try:
                report = await _draft_report(profile, raw_results)
            except Exception:
                logger.exception("Scholar agent: report drafting failed for user %s", user_id)
                return {"status": "report_failed", "user_id": user_id, "query": query}

            alert = AgentAlert(
                user_id=user.id,
                search_query=query,
                report_text=report.report_text,
                matches=[m.model_dump() for m in report.matches],
            )
            session.add(alert)
            await session.commit()
            await session.refresh(alert)

            result = {
                "status": "completed",
                "user_id": user_id,
                "alert_id": str(alert.id),
                "query": query,
                "match_count": len(report.matches),
            }
            logger.info("Scholar agent: %s", result)
            return result
    finally:
        await engine.dispose()
