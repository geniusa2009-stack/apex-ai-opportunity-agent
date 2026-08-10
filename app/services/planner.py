import logging
import math
from datetime import date, datetime

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as grader.py/vision.py: `errors.ClientError` (429s)
# and `errors.ServerError` (5xx) both inherit from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# Beyond this many weeks of runway, we cap the plan at this many weeks and
# instruct the model to group time into broader phases rather than
# producing one thin, task-sparse entry per week of a long calendar gap.
_MAX_PLAN_WEEKS = 12


class RoadmapPastDeadlineError(ValueError):
    """Raised when the opportunity's deadline is not in the future
    relative to `current_date`. A prep roadmap towards a deadline that
    has already passed isn't a meaningful thing to generate -- this is
    checked in Python, before any AI call, rather than asking the model
    to notice and handle it."""


class RoadmapMissingDeadlineError(ValueError):
    """Raised when the opportunity has no deadline at all. There's
    nothing to build a deadline-driven timeline against."""


class RoadmapWeek(BaseModel):
    week_number: int = Field(ge=1, description="1-indexed week of the plan.")
    theme: str = Field(description="The focus of this week, e.g. 'Document Gathering'.")
    tasks: list[str] = Field(
        default_factory=list,
        description="Concrete, actionable steps for this week.",
    )


class RoadmapPlan(BaseModel):
    """Structured output for `generate_roadmap`. Passed as `response_schema`
    with `response_mime_type="application/json"` and validated against
    this shape on our side -- same posture as grader.py/vision.py."""

    weeks: list[RoadmapWeek] = Field(description="The week-by-week preparation plan.")
    notes: str | None = Field(
        default=None,
        description="Optional short caveat about how the plan was adjusted "
        "for the available time -- e.g. that weeks were compressed into "
        "days, or grouped into broader phases. Null if no such adjustment "
        "was needed.",
    )


_ROADMAP_SYSTEM_PROMPT = """\
You are a planning assistant building a week-by-week preparation roadmap \
for a candidate applying to a specific opportunity, ahead of its \
application deadline.

You will be given the exact number of days and weeks of runway remaining \
-- these numbers have already been computed precisely; do not recompute \
or second-guess them, and do not state a different day/week count \
anywhere in your response.

Rules:
- Base the plan only on what the opportunity's description, eligibility \
criteria, and funding details actually say. Do not invent application \
requirements the opportunity didn't mention (e.g. don't assume an essay \
or interview stage exists unless the details say so).
- Each week must have a distinct `theme` and concrete, actionable `tasks` \
-- not vague filler like "keep working on your application."
- Follow the timeline instruction given to you exactly for how many weeks \
to produce and how to structure them (e.g. compressed into days, or \
grouped into broader multi-week phases).
- Set `notes` only if you needed to adjust the structure for the available \
time (very short or very long runway); otherwise leave it null.
"""


def _format_opportunity(opportunity_details: dict) -> str:
    lines = [f"Title: {opportunity_details.get('title', 'Unknown')}"]
    if opportunity_details.get("description"):
        lines.append(f"Description: {opportunity_details['description']}")
    if opportunity_details.get("eligibility_criteria"):
        lines.append(f"Eligibility criteria: {opportunity_details['eligibility_criteria']}")
    if opportunity_details.get("funding_details"):
        lines.append(f"Funding details: {opportunity_details['funding_details']}")
    return "\n".join(lines)


def _parse_date(value: str) -> date:
    # Accept either a bare date ("2026-08-01") or a full ISO timestamp
    # (with a trailing "Z", which fromisoformat only accepts as
    # "+00:00" -- same normalization used elsewhere in this codebase,
    # e.g. app/workers/tasks.py).
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _timeline_guidance(days_remaining: int) -> tuple[str, int]:
    """Returns (guidance text for the prompt, number of weeks to request).

    Computed deterministically in Python rather than left to the model --
    LLM date/interval arithmetic is a known weak spot, and getting this
    wrong would silently mislead a candidate about how much time they
    actually have. The model is only asked to fill in the *content* of
    each week, using the exact counts computed here.
    """
    if days_remaining < 7:
        return (
            f"Only {days_remaining} day(s) remain until the deadline -- less than "
            "a full week. Produce exactly 1 week (week_number=1) with a theme "
            "reflecting urgency (e.g. 'Final Push'), and break `tasks` into "
            "day-by-day or same-day priorities rather than a normal week's worth "
            "of pacing.",
            1,
        )

    weeks_remaining = math.ceil(days_remaining / 7)

    if weeks_remaining <= _MAX_PLAN_WEEKS:
        return (
            f"Exactly {days_remaining} days ({weeks_remaining} weeks) remain until "
            f"the deadline. Produce exactly {weeks_remaining} weeks, numbered 1 "
            f"through {weeks_remaining}, pacing normally across that many weeks.",
            weeks_remaining,
        )

    # Long runway: cap the plan length and have the model group the full
    # remaining time into broader thematic phases instead of one thin
    # week per week of calendar time.
    return (
        f"{days_remaining} days ({weeks_remaining} weeks) remain until the "
        f"deadline -- more than this plan should break into individual weeks. "
        f"Produce exactly {_MAX_PLAN_WEEKS} weeks, numbered 1 through "
        f"{_MAX_PLAN_WEEKS}, where each week represents a broader phase covering "
        f"roughly {days_remaining // _MAX_PLAN_WEEKS} days of real time -- state "
        "in `notes` that weeks here represent multi-week phases, not literal "
        "individual weeks.",
        _MAX_PLAN_WEEKS,
    )


class PlannerGateway:
    """Async wrapper around Gemini roadmap generation via the
    `google-genai` SDK -- same package/model-substitution rationale as
    grader.py and vision.py (gemini-1.5-flash / google-generativeai are
    both stale; see the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase (AIGateway, VisionGateway, GraderGateway).
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_roadmap(self, opportunity_details: dict, current_date: str) -> RoadmapPlan:
        """Generate a week-by-week prep roadmap for `opportunity_details`
        (expects title/description/eligibility_criteria/funding_details
        plus a `deadline` key, ISO date or timestamp) relative to
        `current_date` (ISO date or timestamp).

        Raises `RoadmapMissingDeadlineError` if there's no deadline to
        plan against, or `RoadmapPastDeadlineError` if that deadline is
        not in the future relative to `current_date`.
        """
        deadline_raw = opportunity_details.get("deadline")
        if not deadline_raw:
            raise RoadmapMissingDeadlineError(
                "This opportunity has no deadline on record; a deadline-based "
                "roadmap can't be generated."
            )

        today = _parse_date(current_date)
        deadline = _parse_date(deadline_raw)
        days_remaining = (deadline - today).days

        if days_remaining <= 0:
            raise RoadmapPastDeadlineError(
                f"This opportunity's deadline ({deadline.isoformat()}) is not in "
                f"the future relative to {today.isoformat()}."
            )

        guidance, _num_weeks = _timeline_guidance(days_remaining)

        client = self._client()

        prompt = (
            f"{_format_opportunity(opportunity_details)}\n\n"
            f"Today's date: {today.isoformat()}\n"
            f"Application deadline: {deadline.isoformat()}\n"
            f"Timeline instruction: {guidance}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_planner_model,
            contents=[_ROADMAP_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=RoadmapPlan,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for roadmap generation")

        return RoadmapPlan.model_validate_json(response.text)


planner_gateway = PlannerGateway()
