import logging

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


class NoAlumniCandidatesError(ValueError):
    """Raised when `alumni_profiles` is empty -- there's nothing to match
    against. Checked in Python before any AI call, same posture as
    planner.py's `RoadmapMissingDeadlineError`: a precondition problem,
    not an AI-gateway failure."""


class MatchResult(BaseModel):
    """Structured output for `match_alumni_and_draft`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here.

    `best_match_alumnus_id` is typed as `str`, not `int` -- the ask said
    "integer/string", but every ID in this schema (Opportunity, User, and
    now Alumnus) is a UUID, not an int, matching the same UUID-not-int
    precedent already established for Feature 8's recommendations.py.
    """

    best_match_alumnus_id: str = Field(
        description="The `id` (UUID string) of the single best-matched "
        "alumnus, copied exactly from the candidate list provided -- do "
        "not invent an id that wasn't in the candidate list."
    )
    matching_reason: str = Field(
        description="A short sentence explaining why this alumnus is the "
        "best match, e.g. 'Both graduated from Cairo University with a "
        "major in Computer Science.'"
    )
    icebreaker_message: str = Field(
        description="A polite, non-spammy outreach message the student "
        "can send this alumnus on LinkedIn or by email, ready to send "
        "with minimal editing."
    )
    networking_tips: list[str] = Field(
        default_factory=list,
        description="2-3 quick, concrete tips on approaching this "
        "specific alumnus professionally.",
    )


_ALUMNI_MATCHMAKER_SYSTEM_PROMPT = """\
You are a networking strategist helping a student identify the most relevant \
alumnus from a candidate list, and draft a first outreach message to them.

You will be given the student's own profile, a numbered candidate list of \
alumni (each with an `id` and whatever profile fields are on record for \
them), and the opportunity/scholarship context they're all connected to.

Your job:
1. Evaluate the student's profile against EVERY candidate in the alumni \
list. Pick the single best match, primarily based on shared major, \
university, or country -- in that rough order of relevance, but use \
judgment if one candidate is a much stronger overall fit on other stated \
details (e.g. current position closely related to the student's field).
2. Copy that candidate's `id` value exactly into `best_match_alumnus_id` -- \
do not alter it, and do not invent an id that wasn't in the list.
3. Write a short, polite, non-spammy outreach message the student could \
send this alumnus on LinkedIn or by email to start a conversation.
4. Give 2-3 concrete networking tips specific to this alumnus and this \
outreach, not generic advice.

Rules:
- Only pick from the candidate list you were given -- never invent a "best \
match" alumnus who wasn't in the list.
- Use ONLY the information actually given about the student and the \
matched alumnus. Do not invent a shared connection, shared experience, \
specific project, or credential that wasn't stated -- a fabricated detail \
in a real outreach message would be caught immediately and damage the \
student's credibility.
- The `icebreaker_message` must sound like a specific, informed student, \
not a mail-merge template. Avoid generic cold-outreach cliches ("I hope \
this message finds you well", "I came across your profile and...").
- Keep the message concise -- 2-4 short paragraphs at most.
- `matching_reason` must be a single short sentence, not a paragraph.
"""


def _format_student_profile(student_profile: dict) -> str:
    lines = []
    for label, key in (
        ("Name", "full_name"),
        ("Major", "major"),
        ("Degree level", "degree_level"),
        ("Graduation year", "graduation_year"),
        ("Skills", "skills"),
    ):
        value = student_profile.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else "(No profile details provided.)"


def _format_alumni_candidates(alumni_profiles: list[dict]) -> str:
    blocks = []
    for candidate in alumni_profiles:
        lines = [f"id: {candidate.get('id')}"]
        for label, key in (
            ("Name", "full_name"),
            ("University", "university"),
            ("Major", "major"),
            ("Country", "country"),
            ("Degree level", "degree_level"),
            ("Current position", "current_position"),
            ("Bio", "bio"),
        ):
            value = candidate.get(key)
            if value:
                lines.append(f"{label}: {value}")
        blocks.append("\n".join(lines))
    return "\n\n".join(blocks)


def _format_opportunity(opportunity_details: dict) -> str:
    lines = [f"Title: {opportunity_details.get('title', 'Unknown')}"]
    if opportunity_details.get("description"):
        lines.append(f"Description: {opportunity_details['description']}")
    if opportunity_details.get("funding_details"):
        lines.append(f"Funding details: {opportunity_details['funding_details']}")
    if opportunity_details.get("country"):
        lines.append(f"Country: {opportunity_details['country']}")
    return "\n".join(lines)


class AlumniMatchmakerGateway:
    """Async wrapper around Gemini-based alumni matching + icebreaker
    drafting via the `google-genai` SDK -- same package/model-
    substitution rationale as every other Gemini-backed service in this
    codebase (gemini-1.5-flash / google-generativeai are both stale; see
    the README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def match_alumni_and_draft(
        self,
        student_profile: dict,
        alumni_profiles: list[dict],
        opportunity_details: dict,
    ) -> MatchResult:
        """Match `student_profile` against `alumni_profiles` (each dict
        must include an `id` key) and draft an icebreaker message to the
        best match, in the context of `opportunity_details`.

        Raises `NoAlumniCandidatesError` if `alumni_profiles` is empty.
        """
        if not alumni_profiles:
            raise NoAlumniCandidatesError(
                "No alumni candidates were provided to match against."
            )

        client = self._client()

        prompt = (
            f"Student profile:\n{_format_student_profile(student_profile)}\n\n"
            f"Opportunity context:\n{_format_opportunity(opportunity_details)}\n\n"
            f"Alumni candidates:\n{_format_alumni_candidates(alumni_profiles)}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_alumni_matchmaker_model,
            contents=[_ALUMNI_MATCHMAKER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=MatchResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for alumni matchmaking")

        return MatchResult.model_validate_json(response.text)


alumni_matchmaker_gateway = AlumniMatchmakerGateway()
