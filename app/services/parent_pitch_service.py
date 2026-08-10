import logging
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.models.parent_pitch import ParentPitch
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class PitchGenerationError(Exception):
    """Raised when the Gemini pitch/letter generation call fails."""


class GeneratedPitchData(BaseModel):
    """Structured output for `generate_pitch`, per the ask's exact field
    list. Also the shape persisted as-is into `ParentPitch.generated_pitch_data`."""

    conversation_tactics: list[str] = Field(
        min_length=3,
        max_length=3,
        description="Exactly 3 practical tips on how and when to open "
        "this conversation with the parents -- including concrete advice "
        "on timing, setting, body language, and tone (e.g. choosing a "
        "calm moment after a family meal rather than in the middle of an "
        "argument, sitting rather than standing, speaking softly and "
        "patiently).",
    )
    key_reassurance_points: list[str] = Field(
        min_length=3,
        max_length=3,
        description="Exactly 3 bullet points packed with logic and "
        "factual arguments SPECIFICALLY tailored to the parents' stated "
        "primary concern -- e.g. if the concern is safety, cover campus "
        "security, student accommodation, and local community/cultural "
        "support networks; if it's a different concern, address that "
        "concern's own most persuasive, concrete facts instead.",
    )
    financial_reassurance: str = Field(
        description="A brief, clear explanation proving the scholarship "
        "is fully funded (tuition, living costs, etc. as applicable) and "
        "will not burden the family's budget -- concrete and specific, "
        "not vague reassurance."
    )
    formal_arabic_letter: str = Field(
        description="A beautifully drafted, incredibly polite, and "
        "emotionally moving letter written ENTIRELY in formal Modern "
        "Standard Arabic (فصحى), addressed warmly to the parents (e.g. "
        "opening with something like 'أمي الغالية وأبي العزيز...'), "
        "explaining the opportunity with respect and humility, "
        "acknowledging their concerns, and promising to make them proud. "
        "Must read as genuine, fluent, emotionally resonant Arabic prose "
        "-- not a translated or stilted rendering."
    )


_PARENT_PITCH_SYSTEM_PROMPT = """\
You are an empathetic Arab family counselor and communications expert, \
deeply experienced in helping students navigate family approval for \
studying abroad -- especially in the context of Middle Eastern family \
dynamics, where family approval is often the hardest part of accepting a \
scholarship.

You will be given: the scholarship name, the target country, and the \
parents' primary concern about this opportunity.

Your job is to produce content that is exceptionally respectful of the \
family, warm, and culturally aware -- never dismissive of the parents' \
concern, never framing the parents as an obstacle to overcome, but as \
people who love their child and deserve a thoughtful, honest, reassuring \
case. Every part of your output must be SPECIFICALLY tailored to the \
stated `primary_concern` -- generic reassurance that could apply to any \
concern is not acceptable.

Produce:
1. `conversation_tactics`: exactly 3 practical, concrete tips for how and \
when to open this conversation -- timing, setting, tone, body language.
2. `key_reassurance_points`: exactly 3 specific, factual, logic-driven \
points that directly address the stated concern.
3. `financial_reassurance`: a clear, concrete explanation that the \
scholarship is fully funded and won't burden the family.
4. `formal_arabic_letter`: a complete letter written ENTIRELY in formal, \
polite Modern Standard Arabic (فصحى), addressed warmly to the parents, \
explaining the opportunity, honoring their concern, and promising to make \
them proud. This must be genuine, fluent, emotionally resonant Arabic \
prose -- written natively in Arabic, not a stiff translation from \
English phrasing.

Above all: be respectful, warm, and honest. Never exaggerate or promise \
anything the given facts don't support.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_pitch_content(
    scholarship_name: str, target_country: str, primary_concern: str
) -> GeneratedPitchData:
    prompt = (
        f"Scholarship: {scholarship_name}\n"
        f"Target country: {target_country}\n"
        f"Parents' primary concern: {primary_concern}"
    )
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_parent_pitch_model,
            contents=[_PARENT_PITCH_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=GeneratedPitchData,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise PitchGenerationError(str(exc)) from exc

    if not response.text:
        raise PitchGenerationError("Gemini returned no content for parent pitch generation")

    return GeneratedPitchData.model_validate_json(response.text)


async def generate_pitch(
    db: AsyncSession, user_id: UUID, scholarship_name: str, target_country: str, primary_concern: str
) -> ParentPitch:
    """Generates a persuasion strategy + formal Arabic reassurance letter
    via Gemini and persists it. Takes `db: AsyncSession` as an explicit
    first parameter -- same disclosed necessary-plumbing addition
    established across this project's Ecosystem tier (this ask's literal
    signature omits it)."""
    pitch_data = await _generate_pitch_content(scholarship_name, target_country, primary_concern)

    pitch = ParentPitch(
        user_id=user_id,
        scholarship_name=scholarship_name,
        target_country=target_country,
        primary_concern=primary_concern,
        generated_pitch_data=pitch_data.model_dump(),
    )
    db.add(pitch)
    await db.commit()
    await db.refresh(pitch)
    return pitch


async def get_user_pitches(db: AsyncSession, user_id: UUID) -> list[ParentPitch]:
    """Disclosed necessary plumbing behind `GET /parent-pitch/history` --
    same unpaginated "all of this user's rows, most recent first"
    posture already established for `application_tracker.get_user_trackers`."""
    result = await db.execute(
        select(ParentPitch).where(ParentPitch.user_id == user_id).order_by(ParentPitch.created_at.desc())
    )
    return list(result.scalars().all())
