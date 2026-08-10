import logging

from groq import AsyncGroq
from pydantic import BaseModel, Field

from app.core.config import settings
from app.services.groq_client import build_chat_groq, retry_groq_call

logger = logging.getLogger(__name__)


class SpokenAnswerEvaluation(BaseModel):
    """Structured output for `evaluate_spoken_answer`. Produced via
    `ChatGroq.with_structured_output` -- same verified mechanism used by
    every other Groq-backed structured-output feature in this codebase
    (skill_gap.py, interview.py).

    `content_score` is 1-10, not the 0-10 scale used by
    `SkillGapAnalysis`/`AnswerEvaluation` elsewhere -- kept as literally
    specified in this ask rather than harmonized to the rest of the
    project's convention, since the ask explicitly said "1-10" this time."""

    content_score: int = Field(
        ge=1,
        le=10,
        description="Quality of the spoken answer's actual content for "
        "this interview question, 1-10.",
    )
    confidence_feedback: str = Field(
        description="Feedback on delivery/confidence -- filler words "
        "('um', 'like', 'you know'), hesitation, run-on or fragmented "
        "sentence structure -- based on what the transcript actually "
        "shows, not generic public-speaking advice."
    )
    improvement_tips: list[str] = Field(
        default_factory=list,
        description="Concrete, actionable tips specific to this answer "
        "-- not generic interview-prep advice that could follow any answer.",
    )


_EVALUATION_SYSTEM_PROMPT = """\
You are an interview coach evaluating a transcript of a candidate's SPOKEN \
answer to an interview question (transcribed from audio via speech-to-text).

Rules:
- Score `content_score` (1-10) on the substance of what was actually said in \
response to the question -- relevance, specificity, structure.
- For `confidence_feedback`, look specifically for signs of hesitation or \
low confidence THAT ARE VISIBLE IN THE TRANSCRIPT: filler words ("um", \
"uh", "like", "you know"), false starts, run-on or fragmented sentences, \
repeated words/phrases, or a rambling structure without a clear point. \
Quote or reference the actual transcript when pointing these out. If the \
transcript reads clearly and confidently, say so honestly rather than \
inventing hesitation that isn't there.
- Remember this is a speech-to-text transcript, not text the candidate \
typed -- don't penalize normal spoken-language patterns (contractions, \
slightly looser grammar than written prose) as if they were writing errors; \
focus on genuine hesitation/confidence signals and substance, not written- \
prose polish.
- `improvement_tips` must be concrete and specific to this actual answer, \
not generic interview advice that could follow any answer.
"""


class VoiceCoachGateway:
    """Async wrapper around two Groq capabilities:

    - Whisper speech-to-text (`transcribe_audio`), via the raw `groq`
      SDK's `AsyncGroq` client -- LangChain's `ChatGroq` (used elsewhere
      in this codebase) is a chat-completions wrapper only and has no
      audio transcription surface, so this is the one place in this
      codebase that talks to Groq directly rather than through
      LangChain.
    - Structured-output evaluation (`evaluate_spoken_answer`), reusing
      `build_chat_groq`/`retry_groq_call` from `groq_client.py` -- the
      same shared helper `chatbot.py`/`skill_gap.py`/`interview.py` all
      use, rather than a third reimplementation of Groq client/retry
      setup.
    """

    def _whisper_client(self) -> AsyncGroq:
        # Fresh client per call -- same "different event-loop lifecycle"
        # reasoning as every other gateway in this codebase.
        return AsyncGroq(api_key=settings.groq_api_key)

    @retry_groq_call
    async def transcribe_audio(self, audio_file_path: str) -> str:
        """Transcribe the audio file at `audio_file_path` using Groq's
        hosted Whisper endpoint. Expects the file to already exist on
        disk (see voice_coach.py's router for how it gets there and how
        it's cleaned up)."""
        client = self._whisper_client()

        # Groq's audio API is OpenAI-compatible: pass an open file
        # handle directly as `file`. Local disk read via a plain
        # synchronous `open()` here -- same accepted tradeoff as
        # `advanced.py`'s `await file.read()` (Step 3.4): a short local
        # file read is negligible next to the network round-trip that
        # follows, and this codebase doesn't have an async file I/O
        # dependency yet.
        with open(audio_file_path, "rb") as audio_file:
            transcription = await client.audio.transcriptions.create(
                model=settings.groq_whisper_model,
                file=audio_file,
                response_format="text",
            )

        # `response_format="text"` makes the SDK return a plain string
        # directly rather than a `Transcription` object with a `.text`
        # attribute -- handle both defensively in case that changes.
        return transcription if isinstance(transcription, str) else transcription.text

    @retry_groq_call
    async def evaluate_spoken_answer(
        self, transcribed_text: str, interview_question: str
    ) -> SpokenAnswerEvaluation:
        """Evaluate a transcribed spoken answer's content and delivery
        for `interview_question`."""
        llm = build_chat_groq(temperature=0.3)
        structured_llm = llm.with_structured_output(SpokenAnswerEvaluation)

        prompt = (
            f"Interview question: {interview_question}\n\n"
            f"Transcribed spoken answer:\n{transcribed_text}"
        )

        result = await structured_llm.ainvoke(
            [
                {"role": "system", "content": _EVALUATION_SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ]
        )
        if not isinstance(result, SpokenAnswerEvaluation):
            result = SpokenAnswerEvaluation.model_validate(result)
        return result


voice_coach_gateway = VoiceCoachGateway()
