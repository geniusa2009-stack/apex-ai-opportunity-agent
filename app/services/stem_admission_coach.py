import logging

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase:
# `errors.ClientError` (429s) and `errors.ServerError` (5xx) both inherit
# from `errors.APIError`.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class InconsistentPracticeQuestionError(ValueError):
    """Raised when a generated practice question's `correct_answer` does
    not literally match one of its own `options` -- an internal
    self-consistency failure (not a real-world hallucination check like
    alumni_matchmaker.py's/pdf_filler.py's guards, since there's no
    external reference set to validate against here, but the same
    "don't hand the student something broken" spirit: an MCQ whose
    marked answer isn't actually one of the choices is unusable)."""


class PracticeQuestion(BaseModel):
    question_text: str
    # "[list of 4 strings]" in the ask is a literal structural spec for a
    # multiple-choice question (exactly 4 choices), not an "e.g."-style
    # illustrative quantity like `sample_practice_questions`'s "a list of
    # 3 questions" or `recommended_projects`'s "3-4 projects" elsewhere
    # in this project -- those describe how many items to generate
    # (fuzzy, not hard-enforced per established convention), whereas this
    # describes the fixed shape of a single MCQ (exactly 4 options is
    # what makes it a valid multiple-choice question at all). Hard-
    # constrained accordingly.
    options: list[str] = Field(min_length=4, max_length=4)
    correct_answer: str = Field(description="Must exactly match one of the 4 strings in `options`.")
    detailed_explanation: str


class StemPrepPlanResult(BaseModel):
    """Structured output for `generate_stem_prep_plan`. Passed as
    `response_schema` with `response_mime_type="application/json"` and
    validated against this shape on our side -- same posture as every
    other Gemini-backed service here."""

    encouraging_message: str = Field(description="A brief, motivating intro written for a middle schooler.")
    study_focus_plan: list[str] = Field(
        description="A prioritized list of topics to study, based on the student's weak subjects and days left."
    )
    sample_practice_questions: list[PracticeQuestion] = Field(
        description="3 challenging practice questions tailored to IQ/Math/Science STEM entrance exams."
    )
    test_taking_tips: list[str] = Field(
        description="3 practical tips for time management and handling difficult questions."
    )


_STEM_COACH_SYSTEM_PROMPT = """\
You are a specialized tutor for elite STEM high school entrance exams \
(exams that typically test spatial/logical IQ, advanced math, and \
science problem-solving beyond a normal middle school curriculum), \
coaching a middle school student.

You will be given the student's weak subjects and how many days they \
have left to study.

Your job:
1. Write an `encouraging_message` -- brief, warm, and motivating,
written specifically for a middle schooler (not an older student).
2. Build a `study_focus_plan` -- a prioritized list of topics to study,
genuinely tailored to the student's stated weak subjects and realistic
for the number of days they have left (don't recommend a plan that
would require far more study time than they actually have).
3. Write exactly 3 `sample_practice_questions`, each a genuinely
challenging IQ/Math/Science multiple-choice question appropriate for a
STEM high school entrance exam and for a middle-school-aged student.
Each question must have exactly 4 `options`, a `correct_answer` that is
copied EXACTLY (character-for-character) from one of those 4 options,
and a `detailed_explanation` of why that answer is correct. Double-check
your own math/logic before answering -- an incorrect `correct_answer` is
the single worst failure mode for this feature, since a wrong answer
actively misleads a student studying from it.
4. Write exactly 3 `test_taking_tips` -- practical, actionable advice for
time management and handling difficult questions under exam conditions.

Rules:
- Verify every practice question's `correct_answer` is genuinely,
verifiably correct and copied exactly from its own `options` list --
never guess or approximate.
- Keep everything age-appropriate, encouraging, and free of unnecessary
pressure or discouraging language -- this is a middle schooler, not an
adult.
- Do not invent a specific named exam, school, or scoring detail that
may not actually exist -- speak in general, honest terms about STEM
entrance exams as a category.
"""


def _format_prompt(weak_subjects: list[str], study_days_left: int) -> str:
    return (
        f"Weak subjects: {', '.join(weak_subjects)}\n"
        f"Study days left: {study_days_left}"
    )


def _validate_questions_self_consistent(questions: list[PracticeQuestion]) -> None:
    """Raise `InconsistentPracticeQuestionError` if any question's
    `correct_answer` isn't literally one of its own `options` -- see the
    class docstring for why this is a self-consistency check rather than
    a real-world-fact hallucination guard."""
    for i, question in enumerate(questions):
        if question.correct_answer not in question.options:
            raise InconsistentPracticeQuestionError(
                f"Practice question {i} has correct_answer {question.correct_answer!r} "
                f"which is not among its own options {question.options!r}"
            )


class StemAdmissionCoachGateway:
    """Async wrapper around Gemini-based STEM school admission coaching
    via the `google-genai` SDK -- same package/model-substitution
    rationale as every other Gemini-backed service in this codebase
    (gemini-1.5-flash / google-generativeai are both stale; see the
    README note on this from Step 3.4)."""

    def _client(self) -> genai.Client:
        # Fresh client per call -- same reasoning as every other gateway
        # in this codebase.
        return genai.Client(api_key=settings.gemini_api_key)

    @_retry_gemini_call
    async def generate_stem_prep_plan(
        self, weak_subjects: list[str], study_days_left: int
    ) -> StemPrepPlanResult:
        """Generate a customized STEM high school entrance exam prep plan
        and practice questions for a middle school student, based on
        their weak subjects and how many days they have left to study."""
        client = self._client()

        prompt = _format_prompt(weak_subjects, study_days_left)

        response = await client.aio.models.generate_content(
            model=settings.gemini_stem_coach_model,
            contents=[_STEM_COACH_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=StemPrepPlanResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for STEM prep plan generation")

        result = StemPrepPlanResult.model_validate_json(response.text)
        _validate_questions_self_consistent(result.sample_practice_questions)
        return result


stem_admission_coach_gateway = StemAdmissionCoachGateway()
