import asyncio
import base64
import io
import logging
import os
import tempfile

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from gtts import gTTS
from gtts.tts import gTTSError
from pydantic import BaseModel, Field
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.services.voice_coach import voice_coach_gateway

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


class VoiceSynthesisError(RuntimeError):
    """Raised when gTTS fails to reach Google's text-to-speech endpoint
    (`gtts.tts.gTTSError`) -- distinct from a generic AI-gateway failure
    so the router can log it specifically. Confirmed to occur for real
    in this sandbox (network egress to Google Translate's TTS endpoint is
    blocked here, the same situation Advanced Feature 10 hit with
    DuckDuckGo) -- see this step's README section for the honest
    disclosure on what could and couldn't be tested end-to-end."""


class VoiceReplyResult(BaseModel):
    """Structured output for the Gemini reply-generation step. Wraps a
    single string in a `BaseModel` -- Gemini's `response_schema` is used
    this way (a `BaseModel`, not a bare primitive type) by every other
    Gemini-backed service in this codebase, so this keeps that same
    convention rather than introducing a first-of-its-kind bare-string
    schema at this late a stage, even though the underlying content here
    really is just one string."""

    reply_text: str = Field(description="The advisor's short, spoken-friendly reply.")


_VOICE_SCHOLAR_SYSTEM_PROMPT = (
    "You are a warm, supportive academic advisor chatting with a student "
    "over a voice call. Keep your response friendly, helpful, and very "
    "concise (maximum 2-3 short sentences) so it sounds natural when "
    "spoken back."
)


class VoiceScholarGateway:
    """Async wrapper around a 3-step voice-to-voice loop: Groq Whisper
    (STT) -> Gemini (LLM reply) -> gTTS (TTS). Reuses two already-built
    pieces rather than re-implementing them:

    - STT: `voice_coach_gateway.transcribe_audio` (Step 3.16) -- the raw
      `groq.AsyncGroq` client for Whisper transcription already lives
      there, since `ChatGroq` (LangChain's wrapper, used everywhere else
      in this codebase) has no audio surface. Reusing it here avoids a
      second copy of that same client/retry setup.
    - LLM: a fresh `google-genai` call, same fresh-client-per-call +
      `@_retry_gemini_call` pattern as every other Gemini-backed service
      since Step 3.4.
    - TTS: `gTTS`, new to this step -- see `_synthesize_speech` for the
      blocking-call handling.
    """

    def _gemini_client(self) -> genai.Client:
        return genai.Client(api_key=settings.gemini_api_key)

    async def _transcribe(self, audio_bytes: bytes) -> str:
        """Writes `audio_bytes` to a temp file (Groq's client needs a
        real file path/handle, not raw bytes) and reuses
        `voice_coach_gateway.transcribe_audio` for the actual Whisper
        call. Same `tempfile.NamedTemporaryFile(delete=False)` +
        guaranteed `finally` cleanup pattern as `voice_coach.py`'s router
        (Step 3.16) -- kept here (inside the service) rather than in this
        step's router, since the ask's service signature takes raw
        `audio_bytes`, not a path, making temp-file lifecycle this
        method's own responsibility end-to-end."""
        tmp = tempfile.NamedTemporaryFile(suffix=".wav", delete=False)
        tmp_path = tmp.name
        try:
            tmp.write(audio_bytes)
            tmp.close()
            return await voice_coach_gateway.transcribe_audio(tmp_path)
        finally:
            os.remove(tmp_path)

    @_retry_gemini_call
    async def _generate_reply(self, user_transcript: str) -> str:
        client = self._gemini_client()

        response = await client.aio.models.generate_content(
            model=settings.gemini_voice_scholar_model,
            contents=[_VOICE_SCHOLAR_SYSTEM_PROMPT, user_transcript],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=VoiceReplyResult,
            ),
        )

        if not response.text:
            raise ValueError("Gemini returned no content for voice scholar reply generation")

        return VoiceReplyResult.model_validate_json(response.text).reply_text

    def _synthesize_speech_sync(self, text: str) -> bytes:
        """Blocking gTTS call -- see `_synthesize_speech`'s
        `asyncio.to_thread` wrapping below."""
        try:
            tts = gTTS(text=text, lang="en")
            buffer = io.BytesIO()
            tts.write_to_fp(buffer)
            return buffer.getvalue()
        except gTTSError as exc:
            raise VoiceSynthesisError(f"Text-to-speech synthesis failed: {exc}") from exc

    async def _synthesize_speech(self, text: str) -> bytes:
        """gTTS makes a real, blocking network call under the hood (no
        async API) -- wrapped in `asyncio.to_thread` so it doesn't stall
        the FastAPI event loop, same pattern already used for
        `youtube_transcript_api` (Step 3.19), `ddgs` (Step 3.24), and
        `pypdf` (Step 3.29)."""
        return await asyncio.to_thread(self._synthesize_speech_sync, text)

    async def voice_to_voice_chat(self, audio_bytes: bytes) -> dict:
        """Runs the full STT -> LLM -> TTS loop on `audio_bytes`.

        Returns a plain `dict` (not a Pydantic model), per the ask's
        explicit "Return a dictionary containing..." instruction -- the
        one service in this codebase that deliberately returns a bare
        dict rather than a validated model instance, kept that way to
        match the literal ask rather than "improving" it unasked.
        """
        user_transcript = await self._transcribe(audio_bytes)
        ai_response_text = await self._generate_reply(user_transcript)
        audio_bytes_out = await self._synthesize_speech(ai_response_text)

        return {
            "user_transcript": user_transcript,
            "ai_response_text": ai_response_text,
            "audio_base64": base64.b64encode(audio_bytes_out).decode("ascii"),
        }


voice_scholar_gateway = VoiceScholarGateway()
