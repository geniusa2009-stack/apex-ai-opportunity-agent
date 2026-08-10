import logging
from datetime import date, datetime

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

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


class InsufficientHistoricalDataError(ValueError):
    """Raised when `historical_dates` is empty -- there is nothing to
    detect a pattern from at all. Checked in Python before any AI call,
    same "precondition problem, not an AI-gateway failure" posture as
    `planner.py`'s `RoadmapMissingDeadlineError`. A single data point is
    still ALLOWED through (the router's synthetic-estimate path always
    provides exactly one) -- see the module docstring below on how that
    case is handled via a forced-Low-confidence instruction rather than
    a hard error."""


class TrendPredictionResult(BaseModel):
    """Structured output for `predict_upcoming_cycle`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    `predicted_open_date`/`predicted_close_date` are plain `str`, not a
    `date` type -- the ask explicitly allows "ISO-8601 string YYYY-MM-DD
    OR estimated range," and a strict `date` type would break the moment
    the model (correctly, per the ask) returns a range like "early
    September 2027" instead of a single date. `confidence_level` is a
    plain `str` (not a hard `Literal`) for the same "e.g." reasoning
    already applied to `final_decision` in `peer_review.py` (Legendary
    Feature 4) -- the ask gave "High"/"Medium"/"Low" as examples, not
    necessarily an exhaustive enum.
    """

    predicted_open_date: str = Field(
        description="Predicted opening date for the upcoming cycle -- "
        "ISO-8601 YYYY-MM-DD, or a short estimated range if a single "
        "exact date isn't confidently determinable."
    )
    predicted_close_date: str = Field(
        description="Predicted closing/deadline date for the upcoming "
        "cycle -- same format flexibility as predicted_open_date."
    )
    confidence_level: str = Field(
        description="Typically 'High', 'Medium', or 'Low' -- how "
        "confident this prediction is, given the amount and consistency "
        "of the historical data actually available."
    )
    analysis_reasoning: str = Field(
        description="Brief explanation of the pattern found (or the lack "
        "of one), e.g. 'Historically opens the first week of September "
        "with an average application window of 65 days.'"
    )
    preparation_timeline: list[str] = Field(
        default_factory=list,
        description="Suggested milestones ahead of the predicted open/close "
        "dates, e.g. 'Start requesting recommendation letters by August 1.'",
    )


def _parse_date(value: str) -> date:
    # Accept either a bare date or a full ISO timestamp (with a trailing
    # "Z") -- same normalization used elsewhere in this codebase (e.g.
    # planner.py, app/workers/tasks.py).
    return datetime.fromisoformat(value.replace("Z", "+00:00")).date()


def _annotate_entry(entry: dict) -> dict:
    """Precomputes the deterministic, checkable facts for one historical
    entry (duration, weekday, day-of-month for both dates) -- handed to
    the model as given facts rather than left for it to (mis)calculate.
    Mirrors `planner.py`'s `_timeline_guidance`: LLM date/interval
    arithmetic is a known weak spot, so anything computable in Python is
    computed in Python, and the model is only asked to do the part it's
    actually suited for -- recognizing/describing the pattern, not the
    arithmetic underneath it."""
    annotated = dict(entry)
    open_raw = entry.get("open_date")
    close_raw = entry.get("close_date")

    if open_raw:
        open_d = _parse_date(open_raw)
        annotated["open_weekday"] = open_d.strftime("%A")
        annotated["open_day_of_month"] = open_d.day
    if close_raw:
        close_d = _parse_date(close_raw)
        annotated["close_weekday"] = close_d.strftime("%A")
        annotated["close_day_of_month"] = close_d.day
    if open_raw and close_raw:
        annotated["duration_days"] = (close_d - open_d).days

    return annotated


def _format_historical_dates(annotated_entries: list[dict]) -> str:
    lines = []
    for e in annotated_entries:
        parts = [f"Year {e.get('year', 'unknown')}"]
        if e.get("open_date"):
            parts.append(f"opened {e['open_date']} ({e.get('open_weekday')}, day {e.get('open_day_of_month')} of month)")
        if e.get("close_date"):
            parts.append(f"closed {e['close_date']} ({e.get('close_weekday')}, day {e.get('close_day_of_month')} of month)")
        if "duration_days" in e:
            parts.append(f"duration {e['duration_days']} days")
        lines.append(" -- ".join(parts))
    return "\n".join(lines)


def _build_facts_block(annotated_entries: list[dict]) -> str:
    """Deterministically computed facts -- the model is told not to
    recompute these itself, same instruction pattern as
    `planner.py`'s roadmap prompt."""
    durations = [e["duration_days"] for e in annotated_entries if "duration_days" in e]
    years = [e["year"] for e in annotated_entries if e.get("year") is not None]

    facts = [f"Number of historical data points provided: {len(annotated_entries)}."]
    if durations:
        avg_duration = sum(durations) / len(durations)
        facts.append(f"Average application window across provided years: {avg_duration:.1f} days (already computed -- do not recalculate).")
    if years:
        target_cycle_year = max(years) + 1
        facts.append(f"Target upcoming cycle to predict: {target_cycle_year} (already computed as the year after the most recent historical entry -- do not recalculate).")
    if len(annotated_entries) == 1:
        facts.append(
            "Only ONE historical data point is available. A single data "
            "point cannot establish a real trend or confirm a recurring "
            "pattern -- confidence_level MUST be 'Low' regardless of how "
            "clean that one data point looks, and analysis_reasoning must "
            "say so explicitly rather than implying a multi-year pattern "
            "was detected."
        )

    return "\n".join(facts)


_TREND_PREDICTOR_SYSTEM_PROMPT = """\
You are an analyst predicting when a recurring scholarship/opportunity is \
likely to open and close for its upcoming application cycle, based on its \
historical opening/closing date pattern.

You will be given: (1) a list of historical cycles with their open/close \
dates, each already annotated with its weekday, day-of-month, and \
duration in days, and (2) a block of deterministically pre-computed facts \
(average duration, the target cycle year to predict) that you must treat \
as given and correct -- do NOT recompute or second-guess them, and do not \
state a different number anywhere in your response.

Your job:
1. Look for a real pattern across the historical entries -- e.g. does it \
consistently open on the first Monday of a given month, or close roughly \
the same number of days after opening each year? Only claim a pattern \
exists if the data actually supports it.
2. Predict `predicted_open_date` and `predicted_close_date` for the \
target cycle year given in the facts block, consistent with whatever \
pattern (or lack of one) you found.
3. Set `confidence_level` honestly based on how much and how consistent \
the historical data actually is -- more years and a tighter, more \
consistent pattern warrants higher confidence; sparse or inconsistent \
data warrants lower confidence. Follow the facts block's explicit \
instruction if it tells you confidence MUST be capped at a certain level.
4. Write `analysis_reasoning`: a brief, honest explanation of the actual \
pattern found (or explicitly note if there isn't enough data to find one).
5. Give a `preparation_timeline`: concrete milestones timed relative to \
your predicted dates (e.g. "Start requesting recommendation letters by \
August 1" if you predicted an early-September opening).

Rules:
- Never state a duration, year, or date-arithmetic result that contradicts \
the pre-computed facts block.
- Do not fabricate additional historical years or dates beyond what was \
actually given.
- Be honest about uncertainty -- a prediction from thin data should read \
as a reasoned estimate, not a confident forecast.
"""


class TrendPredictorGateway:
    """Async wrapper around Gemini-based scholarship cycle trend
    prediction via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def predict_upcoming_cycle(self, historical_dates: list[dict]) -> TrendPredictionResult:
        """Predict the upcoming cycle's open/close dates from
        `historical_dates` (list of `{"year": int, "open_date": str,
        "close_date": str}` dicts). Raises `InsufficientHistoricalDataError`
        if the list is empty."""
        if not historical_dates:
            raise InsufficientHistoricalDataError(
                "No historical date data was provided -- a prediction "
                "can't be made from nothing."
            )

        annotated = [_annotate_entry(e) for e in historical_dates]
        facts_block = _build_facts_block(annotated)
        history_block = _format_historical_dates(annotated)

        client = self._client()

        prompt = (
            f"Historical cycles:\n{history_block}\n\n"
            f"Pre-computed facts (do not recalculate):\n{facts_block}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_trend_predictor_model,
            contents=[_TREND_PREDICTOR_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=TrendPredictionResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for scholarship trend prediction")

        return TrendPredictionResult.model_validate_json(response.text)


trend_predictor_gateway = TrendPredictorGateway()
