import logging

from pydantic import BaseModel, Field

from app.services.groq_client import build_chat_groq, retry_groq_call

logger = logging.getLogger(__name__)


class SkillGapAnalysis(BaseModel):
    """Structured output comparing a user's background against a specific
    opportunity's stated requirements. Produced via `ChatGroq.with_structured_output`
    (function-calling based) -- verified in the sandbox to reliably return
    a validated instance of this model rather than free text to parse."""

    overall_fit_score: int = Field(
        ge=0,
        le=10,
        description="How well the candidate's background matches this "
        "opportunity's stated requirements, 0-10.",
    )
    matched_skills: list[str] = Field(
        default_factory=list,
        description="Skills, experience, or qualifications the candidate's "
        "background shows that align with what this opportunity is looking for.",
    )
    missing_skills: list[str] = Field(
        default_factory=list,
        description="Skills, experience, or qualifications the opportunity "
        "appears to want that the candidate's background doesn't evidence.",
    )
    recommendations: list[str] = Field(
        default_factory=list,
        description="Concrete, actionable suggestions -- specific experience "
        "to pursue, or existing strengths to highlight more prominently in "
        "an application -- not generic career advice.",
    )
    summary: str = Field(description="A short (2-4 sentence) plain-language summary of the overall fit.")


_SKILL_GAP_SYSTEM_PROMPT = """\
You are a career advisor comparing a candidate's background against a specific \
opportunity's stated requirements.

Rules:
- Base `matched_skills` and `missing_skills` only on what's actually stated in \
the opportunity's description/eligibility criteria and the candidate's \
background below. Do not invent requirements the opportunity didn't mention, \
and do not credit the candidate with skills their background doesn't evidence.
- `recommendations` must be concrete and actionable, not generic advice that \
could apply to any candidate.
- Score honestly in `overall_fit_score` -- a low score paired with clear, \
actionable `recommendations` is more useful to the candidate than an inflated one.
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


@retry_groq_call
async def analyze_skill_gap(opportunity_details: dict, user_background: str) -> SkillGapAnalysis:
    """Compare `user_background` (free text) against `opportunity_details`
    (title/description/eligibility_criteria/funding_details) and return a
    structured gap analysis."""
    llm = build_chat_groq(temperature=0.2)
    structured_llm = llm.with_structured_output(SkillGapAnalysis)

    prompt = (
        f"{_format_opportunity(opportunity_details)}\n\n"
        f"Candidate's background (as provided by the candidate):\n{user_background}"
    )

    result = await structured_llm.ainvoke(
        [
            {"role": "system", "content": _SKILL_GAP_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    # `with_structured_output`'s return type is documented as `dict | BaseModel`
    # depending on method/model behavior -- defensively re-validate rather
    # than assume which one came back.
    if not isinstance(result, SkillGapAnalysis):
        result = SkillGapAnalysis.model_validate(result)
    return result
