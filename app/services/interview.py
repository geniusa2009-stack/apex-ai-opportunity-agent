import logging

from pydantic import BaseModel, Field

from app.services.groq_client import build_chat_groq, retry_groq_call

logger = logging.getLogger(__name__)


class InterviewQuestions(BaseModel):
    """Wrapper so question generation can use `with_structured_output` too,
    rather than parsing a numbered list back out of free text. No strict
    `min_length`/`max_length` on `questions` -- if the model returns 2 or 6
    instead of the requested 3-5, that's a minor prompt-compliance miss,
    not something worth hard-failing the whole request over (Pydantic would
    raise a ValidationError inside `with_structured_output` and the caller
    would see a 502 for what's really a "good enough" response)."""

    questions: list[str] = Field(
        description="3 to 5 interview questions tailored specifically to this opportunity."
    )


class AnswerEvaluation(BaseModel):
    """Structured output for `evaluate_answer`. Produced via
    `ChatGroq.with_structured_output` -- verified in the sandbox to return
    a validated instance of this model directly rather than free text
    requiring manual JSON parsing (the "precise JSON extraction" this
    feature specifically asked for)."""

    score: int = Field(ge=0, le=10, description="Quality of the answer for this specific question/opportunity, 0-10.")
    feedback: str = Field(description="Specific, constructive feedback on what the candidate actually said.")
    ideal_answer: str = Field(
        description="A strong example answer to this question for this opportunity -- "
        "a model answer demonstrating what a good response covers, not a claim "
        "about the real candidate's unstated personal history."
    )


_QUESTION_GEN_SYSTEM_PROMPT = """\
You are an experienced interviewer preparing mock interview questions for a \
candidate applying to a specific scholarship, internship, volunteer position, \
or course.

Rules:
- Generate 3-5 questions genuinely specific to THIS opportunity -- reference \
its actual focus area, stated eligibility, or purpose, rather than generic \
"tell me about yourself" filler that could apply to any opportunity.
- Mix question types where it makes sense: motivation/fit for this specific \
opportunity, a relevant scenario or behavioral question, and one about \
background relevant to the opportunity's actual focus.
"""

_EVALUATION_SYSTEM_PROMPT = """\
You are an experienced interviewer evaluating a candidate's mock interview \
answer for a specific opportunity.

Rules:
- Score honestly (0-10) based on relevance, specificity, and how well the \
answer addresses the question for THIS opportunity. A vague or generic \
answer should not receive an inflated score.
- `feedback` must be specific to what the candidate actually said -- not \
generic interview advice that could follow any answer.
- `ideal_answer` should be a realistic, well-structured example answer \
demonstrating what a strong response covers for this question and \
opportunity. You don't know the real candidate's actual background beyond \
what's in their answer, so don't present it as a claim about them --
frame it as an example, not their own life story.
"""


def _format_opportunity(opportunity_details: dict) -> str:
    lines = [f"Title: {opportunity_details.get('title', 'Unknown')}"]
    if opportunity_details.get("description"):
        lines.append(f"Description: {opportunity_details['description']}")
    if opportunity_details.get("eligibility_criteria"):
        lines.append(f"Eligibility criteria: {opportunity_details['eligibility_criteria']}")
    return "\n".join(lines)


@retry_groq_call
async def generate_interview_questions(opportunity_details: dict) -> list[str]:
    """Generate 3-5 mock interview questions tailored to `opportunity_details`
    (expects title/description/eligibility_criteria keys)."""
    llm = build_chat_groq(temperature=0.5)
    structured_llm = llm.with_structured_output(InterviewQuestions)

    prompt = f"{_format_opportunity(opportunity_details)}\n\nGenerate 3-5 tailored mock interview questions."

    result = await structured_llm.ainvoke(
        [
            {"role": "system", "content": _QUESTION_GEN_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not isinstance(result, InterviewQuestions):
        result = InterviewQuestions.model_validate(result)
    return result.questions


@retry_groq_call
async def evaluate_answer(question: str, user_answer: str, opportunity_details: dict) -> dict:
    """Evaluate `user_answer` to `question` in the context of
    `opportunity_details`. Returns a plain dict with `score` (0-10),
    `feedback`, and `ideal_answer` keys -- matching the requested return
    shape exactly (the underlying structured call is Pydantic-validated;
    `.model_dump()` is what turns that into the plain dict)."""
    llm = build_chat_groq(temperature=0.2)
    structured_llm = llm.with_structured_output(AnswerEvaluation)

    prompt = (
        f"{_format_opportunity(opportunity_details)}\n\n"
        f"Interview question: {question}\n\n"
        f"Candidate's answer: {user_answer}"
    )

    result = await structured_llm.ainvoke(
        [
            {"role": "system", "content": _EVALUATION_SYSTEM_PROMPT},
            {"role": "user", "content": prompt},
        ]
    )
    if not isinstance(result, AnswerEvaluation):
        result = AnswerEvaluation.model_validate(result)
    return result.model_dump()
