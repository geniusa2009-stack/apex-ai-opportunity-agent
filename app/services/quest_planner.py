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


class Quest(BaseModel):
    quest_id: int = Field(ge=1, description="1-indexed quest number, in the order it should be tackled.")
    title: str = Field(description="An RPG-flavored quest title, e.g. 'Gathering the Scroll of Recommendations'.")
    description: str = Field(description="A short, flavorful but genuinely informative description of this quest.")
    xp_reward: int = Field(
        ge=0,
        description="XP awarded for completing this quest, scaled to its real-world "
        "effort/difficulty -- typically in the 100-500 range (harder/longer quests "
        "earning more), though not a hard cap.",
    )
    estimated_days: int = Field(ge=1, description="Realistic estimated days to complete this quest.")
    actionable_steps: list[str] = Field(
        default_factory=list,
        description="Concrete, real actionable steps to complete this quest -- "
        "the substance behind the flavorful title/description.",
    )


class QuestPlanResult(BaseModel):
    """Structured output for `generate_quest_line`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    quests: list[Quest] = Field(
        description="4-5 progressive quests breaking down the application "
        "journey, in the order they should be tackled."
    )
    academic_level_title: str = Field(
        description="A fun, RPG-flavored title based on the student's "
        "profile, e.g. 'SOP Apprentice' or 'Scholar Knight'."
    )


_QUEST_PLANNER_SYSTEM_PROMPT = """\
You are a Gamification Designer and Academic Coach, turning a stressful, \
tedious scholarship/opportunity application journey into an engaging, \
RPG-like quest line -- without losing any of the real substance of what \
the candidate actually needs to do.

You will be given the opportunity's actual details (description, \
eligibility criteria, funding details) and the candidate's own profile \
(whatever fields are on record).

Your job:
1. Break the REAL application requirements implied by the opportunity's \
own details into 4-5 progressive quests, each building toward being \
ready to submit a strong application. Use RPG-flavored `title`s and \
`description`s (e.g. "Quest 1: Gathering the Scroll of Recommendations", \
"Quest 2: Drafting the Hero's Motivation Statement") -- but every quest's \
`actionable_steps` must be genuinely concrete, real, actionable tasks, \
not just flavor text.
2. Assign each quest an `xp_reward` scaled to its real difficulty/effort \
(a quick task earns less, a substantial one like drafting a full essay \
earns more) and a realistic `estimated_days`.
3. Give the candidate a fun `academic_level_title` based on their actual \
profile (e.g. major, degree level) -- thematic and encouraging, not \
generic.

Rules:
- Base every quest on what the opportunity's own description/eligibility/ \
funding details actually say -- do not invent application requirements \
the opportunity never mentioned (e.g. don't assume an interview stage \
exists unless the details say so).
- Do not invent facts about the candidate beyond what's in their profile.
- Keep the RPG flavor genuinely fun, not corny filler that gets in the \
way of the candidate understanding what to actually do -- the flavor is \
a motivational wrapper around real, useful guidance, not a replacement \
for it.
- Order quests in a sensible progression (e.g. gathering documents before \
drafting essays, drafting before final review).
"""


def _format_student_profile(student_profile: dict) -> str:
    lines = []
    for label, key in (
        ("Major", "major"),
        ("Degree level", "degree_level"),
        ("GPA", "gpa"),
        ("Graduation year", "graduation_year"),
        ("Skills", "skills"),
        ("Target country", "target_country"),
    ):
        value = student_profile.get(key)
        if value:
            lines.append(f"{label}: {value}")
    return "\n".join(lines) if lines else "(No profile details provided.)"


def _format_opportunity(opportunity_details: dict) -> str:
    lines = [f"Title: {opportunity_details.get('title', 'Unknown')}"]
    if opportunity_details.get("description"):
        lines.append(f"Description: {opportunity_details['description']}")
    if opportunity_details.get("eligibility_criteria"):
        lines.append(f"Eligibility criteria: {opportunity_details['eligibility_criteria']}")
    if opportunity_details.get("funding_details"):
        lines.append(f"Funding details: {opportunity_details['funding_details']}")
    return "\n".join(lines)


class QuestPlannerGateway:
    """Async wrapper around Gemini-based gamified quest-line generation
    via the `google-genai` SDK -- same package/model-substitution
    rationale as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_quest_line(
        self, student_profile: dict, opportunity_details: dict
    ) -> QuestPlanResult:
        """Generate a 4-5 quest gamified application roadmap for
        `opportunity_details`, personalized with `student_profile`."""
        client = self._client()

        prompt = (
            f"Opportunity:\n{_format_opportunity(opportunity_details)}\n\n"
            f"Candidate profile:\n{_format_student_profile(student_profile)}"
        )

        response = await client.aio.models.generate_content(
            model=settings.gemini_quest_planner_model,
            contents=[_QUEST_PLANNER_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=QuestPlanResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for quest line generation")

        return QuestPlanResult.model_validate_json(response.text)


quest_planner_gateway = QuestPlannerGateway()
