"""Student Video Profiles & AI-Moderated Upload System -- Service Layer.

`StudentVideoService`, named exactly as this step's own ask, wraps the
five external-integration sub-steps its Celery task (`app/tasks/
student_video.py`) runs in sequence: HLS transcode + IPFS pin (reusing
`media_service.py` directly, not reimplemented), audio extraction (also
via `media_service.py` -- see that module's disclosed addition), Whisper
transcription (OpenAI, NOT the Groq Whisper already used by
`voice_coach.py` -- this ask explicitly names "OpenAI's Whisper-1 API"),
Gemini structured safety/quality evaluation, and an OpenAI summary
embedding.

Mixed sync/async, deliberately, matching the exact same reasoning already
established for the Core Video Media Processing Pipeline: the two
methods that delegate to `media_service.py` (`transcode_and_pin_to_ipfs`,
`extract_audio`) are plain synchronous functions -- `ffmpeg`/`httpx.Client`
under the hood, nothing to gain from an async wrapper -- while
`transcribe_audio`/`evaluate_and_moderate`/`generate_summary_embedding`
are `async def`, using this codebase's established `AsyncOpenAI`/
`google-genai` async client patterns (see `app/services/ai.py`/
`course_service.py`). All five are called from inside one `asyncio.run()`
coroutine in the Celery task, the same bridging pattern used throughout
this codebase -- see that task's own module docstring for the fuller
version of this reasoning.

Automated WebVTT Captions Integration step added two things to this
file, both disclosed here and again at their own definitions:

1. `transcribe_audio` now requests `response_format="verbose_json"` from
   Whisper instead of `"text"` -- a deliberate technical correction, not
   a literal reading of anything this step's ask specified. The ask's
   own captions-endpoint instructions say to "dynamically split the
   transcript text into logical sentences or timed blocks... chunking
   text into 5-7 second intervals dynamically distributed across the
   video's total duration_seconds" -- i.e. estimate timing by proportion
   of a flat string. But Whisper can just tell us the REAL per-segment
   start/end timestamps directly, for free, in the same API call, if
   asked for `verbose_json` instead of plain `"text"`. Building captions
   off invented, evenly-distributed timing when the actual timing is
   available for the same cost would be a strictly worse engineering
   choice, so this switches to the real thing. The naive
   proportional-distribution approach the ask describes is NOT thrown
   away, though -- it's implemented as `_chunk_segment`'s fallback path,
   used only for legacy rows transcribed before this change (`transcript_segments`
   is `NULL`, only flat `transcript_text` + `duration_seconds` exist).
2. `generate_webvtt_captions` (module-level, alongside `search_student_videos`
   for the same "doesn't belong on the stateless external-API-wrapper
   class" reasoning) turns either real segments or the legacy flat-text
   fallback into a valid WebVTT string. See its own docstring.

Student Video Pinata -> Cloudflare R2 Migration step RENAMED this
class's two storage-backend methods and switched their implementation
from Pinata IPFS pinning to Cloudflare R2 uploads (`app/services/
r2_service.py`), disclosed at each renamed method's own docstring below:
`transcode_and_pin_to_ipfs` -> `transcode_and_upload_to_r2`, and
`resolve_thumbnail_cid` -> `resolve_thumbnail_url`. This migration's
scope is explicitly THIS pipeline only -- see `app/core/config.py`'s R2
settings block for why Course/Lesson videos (`media_service.py`,
untouched by this step) stay on Pinata.
"""

import logging
import math
import os
import re

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from openai import APIConnectionError as OpenAIAPIConnectionError
from openai import APITimeoutError as OpenAIAPITimeoutError
from openai import AsyncOpenAI
from openai import RateLimitError as OpenAIRateLimitError
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.schemas.student_videos import StudentVideoOut, StudentVideoSearchResult
from app.services import media_service, r2_service
from app.services.ai import ai_gateway

logger = logging.getLogger(__name__)

# OpenAI's Whisper speech-to-text model name -- hardcoded, not a config
# setting, matching this codebase's existing posture for
# `groq_whisper_model`-style fixed model identifiers: this is the
# specific model this ask names ("Whisper-1"), not one this codebase has
# ever needed to swap independently the way the many `gemini_*_model`
# settings are each kept tunable.
_WHISPER_MODEL = "whisper-1"

# Hybrid Video Thumbnail System addition -- maps the file extensions the
# router validates a custom thumbnail upload against
# (app/api/v1/endpoints/student_videos.py's own `_ALLOWED_THUMBNAIL_EXTENSIONS`)
# to the `Content-Type` `resolve_thumbnail_url` passes to R2 for that
# upload's object (originally Pinata's multipart `file` part -- same
# mapping, same reasoning, just a different destination as of the
# Student Video Pinata -> Cloudflare R2 Migration step). Kept local to
# this module (not imported from the router) -- the router owns the
# user-facing accept/reject VALIDATION decision (a request-shape
# concern), this module owns turning an already-validated file into the
# right upload call (a pipeline-mechanics concern); the two module
# boundaries stay clean at the cost of this one small, intentional
# duplication of "which extensions are thumbnails" between them.
_THUMBNAIL_CONTENT_TYPES = {
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    # Disclosed fix, Security Shield milestone re-pass: `.webp` was
    # missing here even though `app/core/file_validator.py`'s own
    # `ALLOWED_THUMBNAIL_EXTENSIONS` now accepts it -- without this
    # entry, a validated `.webp` thumbnail would fall through to the
    # `"application/octet-stream"` default below and be uploaded to R2
    # with the wrong Content-Type header.
    ".webp": "image/webp",
}

# Same two-provider retry shape as `app/services/ai.py` -- reused
# verbatim rather than re-derived, since it's the exact same "3 attempts,
# exponential backoff, keyed to each SDK's own transient-error classes"
# policy this codebase already established.
_retry_openai_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(
        (OpenAIAPIConnectionError, OpenAIAPITimeoutError, OpenAIRateLimitError)
    ),
)

_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)


class AudioTranscriptionError(Exception):
    """Raised when the OpenAI Whisper-1 transcription call fails."""


class VideoEvaluationError(Exception):
    """Raised when the Gemini safety/quality evaluation call fails."""


class EmbeddingGenerationError(Exception):
    """Raised when the OpenAI summary-embedding call fails."""


class CaptionsUnavailableError(Exception):
    """Raised by `generate_webvtt_captions` when a video has neither real
    segment timestamps nor even a flat transcript + duration to fall back
    on -- i.e. there is nothing to build a caption track from at all."""


class TranscriptSegment(BaseModel):
    """One Whisper-reported speech segment. `start`/`end` are seconds
    from the start of the audio, exactly as Whisper's own `verbose_json`
    response reports them -- not re-derived or estimated."""

    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str


class TranscriptionResult(BaseModel):
    """`transcribe_audio`'s return value. `segments` is Whisper's own
    per-segment timing (may legitimately be empty for a near-silent or
    extremely short clip -- `generate_webvtt_captions` treats an empty
    list the same as `None`). `duration_seconds` comes from the SAME
    `verbose_json` response (the audio track's own duration) -- see the
    module docstring for why this is preferred over a second `ffprobe`
    call against the source video."""

    text: str
    segments: list[TranscriptSegment]
    duration_seconds: float | None = None


class GeminiVideoEvaluation(BaseModel):
    """Structured output Gemini's evaluation call is forced to produce,
    via `response_schema` -- the exact four keys and constraints this
    step's own system prompt specifies, word for word: `score` is
    "integer between 1 and 100" (note: NOT 0-100 -- `StudentVideo.ai_score`'s
    own column allows 0 as a defensive floor, but Gemini itself is never
    asked to return a 0), `strengths`/`growth_points` each "at least 2,"
    `tags` "3-5."
    """

    safety_approved: bool = Field(
        description="False if the content contains vulgarity, hate speech, "
        "or inappropriate non-educational content."
    )
    score: int = Field(ge=1, le=100, description="Explanation clarity, 1-100.")
    summary: str = Field(description="A 2-sentence, highly engaging summary of the topic discussed.")
    strengths: list[str] = Field(min_length=2, description="At least 2 strong points about the explanation.")
    growth_points: list[str] = Field(
        min_length=2, description="At least 2 constructive tips for improvement."
    )
    tags: list[str] = Field(min_length=3, max_length=5, description="3-5 relevant educational tags.")


# Word for word from this step's own ask, save for the closing
# reiteration of the exact-keys requirement (Gemini's `response_schema`
# already enforces that structurally, so restating it in prose too is
# redundant, not a change in meaning).
_EVALUATION_SYSTEM_PROMPT = """\
You are an expert educational quality controller. Analyze this transcribed \
student video content. You must return a strict JSON object with these \
exact keys:
- 'safety_approved': boolean (false if it contains vulgarity, hate speech, \
or inappropriate non-educational content).
- 'score': integer between 1 and 100 representing explanation clarity.
- 'summary': string (a 2-sentence highly engaging summary of the topic \
discussed).
- 'strengths': array of strings (at least 2 strong points about their \
explanation).
- 'growth_points': array of strings (at least 2 constructive tips for \
improvement).
- 'tags': array of strings (3-5 relevant educational tags).
"""


class StudentVideoService:
    """See module docstring for the full sync/async design rationale."""

    def _openai_client(self) -> AsyncOpenAI:
        # Fresh client per call -- same "different event-loop lifecycle"
        # reasoning as every other Gateway/Service in this codebase (see
        # `AIGateway`'s own docstring in app/services/ai.py for the full
        # version).
        return AsyncOpenAI(api_key=settings.openai_api_key)

    def _gemini_client(self) -> genai.Client:
        return genai.Client(api_key=settings.gemini_api_key)

    # -----------------------------------------------------------------
    # Sub-step 1: HLS transcode + R2 upload (sync, reuses media_service
    # for the transcode, r2_service for the upload)
    # -----------------------------------------------------------------

    def transcode_and_upload_to_r2(self, local_filepath: str, hls_output_dir: str, key_prefix: str) -> tuple[str, dict]:
        """RENAMED from `transcode_and_pin_to_ipfs` (Student Video Pinata
        -> Cloudflare R2 Migration step). Runs `media_service.
        transcode_to_hls` then `r2_service.upload_hls_directory_to_r2`
        against the same output directory, returning `(hls_master_url,
        transcode_metadata)` -- `hls_master_url` is now a full,
        directly-playable public R2 URL (see `r2_service.
        upload_hls_directory_to_r2`'s own docstring), not an IPFS CID.

        Deliberately thin -- no logic of its own beyond sequencing two
        already-complete, already-tested functions; see
        `media_service.py` for the ffmpeg mechanics (`HLSTranscodeError`)
        and `r2_service.py` for the upload mechanics
        (`R2ConfigurationError`/`R2UploadError`), both of which propagate
        through this method unchanged (not caught/re-wrapped here) so the
        Celery task's error handling sees exactly which of the two stages
        failed.

        `key_prefix` (renamed from `pin_name`, which was purely a Pinata
        dashboard label with no bearing on the resulting CID) is now a
        REAL, meaningful R2 object key prefix -- e.g.
        `student-videos/{video_id}/hls` -- directly determining where
        this video's HLS tree actually lives in the bucket. See the
        Celery task's own call site for the exact value used.
        """
        transcode_result = media_service.transcode_to_hls(local_filepath, hls_output_dir)
        hls_master_url = r2_service.upload_hls_directory_to_r2(hls_output_dir, key_prefix)
        return hls_master_url, transcode_result

    # -----------------------------------------------------------------
    # Sub-step 2: audio extraction (sync, reuses media_service)
    # -----------------------------------------------------------------

    def extract_audio(self, local_filepath: str, audio_output_path: str) -> str:
        """Thin passthrough to `media_service.extract_audio_track` -- see
        that function's docstring for why it lives there rather than
        here."""
        return media_service.extract_audio_track(local_filepath, audio_output_path)

    # -----------------------------------------------------------------
    # Sub-step 1.5: Hybrid Video Thumbnail System (sync, best-effort)
    # -----------------------------------------------------------------

    def resolve_thumbnail_url(
        self,
        local_video_filepath: str,
        custom_thumbnail_filepath: str | None,
        tmp_dir: str,
        object_key: str,
    ) -> str | None:
        """RENAMED from `resolve_thumbnail_cid` (Student Video Pinata ->
        Cloudflare R2 Migration step). Resolves this video's
        `thumbnail_url` -- the Hybrid Video Thumbnail System's whole
        reason to exist, now uploading to R2 instead of pinning to IPFS.
        Two sources, tried in this order, matching the ask's own "custom
        upload, else FFmpeg fallback" framing exactly:

        1. `custom_thumbnail_filepath` -- the viewer's own uploaded image
           (already saved to disk, already validated as a JPG/PNG under
           5MB by the router), if one was provided. Uploaded to R2
           as-is, no FFmpeg step involved at all -- this IS the "skip the
           extraction step to save CPU cycles" requirement, satisfied
           structurally: `extract_thumbnail_frame` is simply never
           called on this path.
        2. Otherwise, `local_video_filepath` -- the raw uploaded video
           itself. `media_service.extract_thumbnail_frame` grabs the
           frame at the 1-second mark into `tmp_dir` (the SAME
           per-attempt temp directory the caller already uses for HLS
           output/audio extraction -- force-deleted wholesale by the
           Celery task's own `finally: shutil.rmtree(tmp_dir)` on every
           attempt, so this function does not need its own cleanup logic
           for that extracted file), then that frame is uploaded to R2.

        `object_key` (renamed from `pin_name`, a Pinata-only dashboard
        label with no bearing on the resulting CID) is now a REAL R2
        object key -- e.g. `student-videos/{video_id}/thumbnail.jpg` --
        directly determining where this thumbnail actually lives in the
        bucket, passed straight through to `r2_service.upload_file_to_r2`.

        DISCLOSED, DELIBERATE DESIGN CHOICE -- this function NEVER
        raises, same posture as before this migration, just against a
        different storage backend's failure modes. Every failure from
        either path (a corrupt/too-short source video with no frame at
        1s, a transient R2 outage, missing/incomplete R2 credentials --
        `R2ConfigurationError` included) is caught here, logged as a
        warning, and turned into a `None` return -- NOT propagated up to
        the Celery task's own outer try/except (which would otherwise
        treat it as a pipeline-wide failure, triggering a full
        retry-with-backoff of the ENTIRE video -- including the
        already-expensive HLS transcode, Whisper transcription, and
        Gemini moderation steps -- or, on the final attempt, marking an
        otherwise perfectly good video FAILED) just because its
        THUMBNAIL couldn't be produced. The ask's own framing calls this
        a "Hybrid" system meant to give the video a thumbnail one way or
        another -- not a system whose failure should ever be allowed to
        take the whole video down with it. A video with `thumbnail_url =
        None` still plays fine; a video wrongly marked FAILED because of
        a thumbnail problem does not.
        """
        try:
            if custom_thumbnail_filepath is not None:
                _, ext = os.path.splitext(custom_thumbnail_filepath.lower())
                content_type = _THUMBNAIL_CONTENT_TYPES.get(ext, "application/octet-stream")
                return r2_service.upload_file_to_r2(
                    custom_thumbnail_filepath, key=object_key, content_type=content_type
                )

            extracted_path = os.path.join(tmp_dir, "thumbnail.jpg")
            media_service.extract_thumbnail_frame(local_video_filepath, extracted_path)
            return r2_service.upload_file_to_r2(
                extracted_path, key=object_key, content_type="image/jpeg"
            )
        except Exception:
            logger.warning(
                "Thumbnail resolution failed for object_key=%r (video will still be processed; "
                "thumbnail_url will be left null) -- see traceback",
                object_key,
                exc_info=True,
            )
            return None

    # -----------------------------------------------------------------
    # Sub-step 3: Whisper transcription (async, OpenAI)
    # -----------------------------------------------------------------

    @_retry_openai_call
    async def transcribe_audio(self, audio_path: str) -> TranscriptionResult:
        """Transcribes `audio_path` via OpenAI's hosted Whisper-1 model.

        No `language` parameter is passed -- Whisper auto-detects the
        spoken language when it's omitted, which is exactly what
        "supporting English and Arabic dynamically" per this step's ask
        calls for: forcing a fixed `language="en"` (or "ar") would
        actively break the other language, so dynamic detection is the
        only reading of that requirement that supports both at once
        without the caller having to already know which language a given
        video is in before transcribing it.

        `response_format="verbose_json"` -- changed from the original
        Student Video Profiles step's `"text"`, disclosed in this
        module's own docstring under "Automated WebVTT Captions
        Integration." `whisper-1` includes per-segment start/end
        timestamps in `verbose_json` responses by default (no separate
        `timestamp_granularities` parameter needed for segment-level
        granularity -- that parameter only matters for word-level
        timestamps or newer transcription models), at no extra cost over
        plain `"text"`.
        """
        client = self._openai_client()
        try:
            async with client:
                with open(audio_path, "rb") as audio_file:
                    transcription = await client.audio.transcriptions.create(
                        model=_WHISPER_MODEL,
                        file=audio_file,
                        response_format="verbose_json",
                    )
        except (OpenAIAPIConnectionError, OpenAIAPITimeoutError, OpenAIRateLimitError):
            raise
        except Exception as exc:
            raise AudioTranscriptionError(str(exc)) from exc

        text = (getattr(transcription, "text", None) or "").strip()
        if not text:
            raise AudioTranscriptionError("Whisper returned an empty transcript")

        raw_segments = getattr(transcription, "segments", None) or []
        segments = [
            TranscriptSegment(start=float(seg.start), end=float(seg.end), text=cleaned)
            for seg in raw_segments
            if (cleaned := (getattr(seg, "text", "") or "").strip())
        ]

        raw_duration = getattr(transcription, "duration", None)
        return TranscriptionResult(
            text=text,
            segments=segments,
            duration_seconds=float(raw_duration) if raw_duration is not None else None,
        )

    # -----------------------------------------------------------------
    # Sub-step 4: Gemini safety/quality evaluation (async)
    # -----------------------------------------------------------------

    @_retry_gemini_call
    async def evaluate_and_moderate(
        self, transcript: str, academic_context: str | None = None
    ) -> GeminiVideoEvaluation:
        """Sends `transcript` to Gemini with the exact system prompt this
        step specifies, forcing structured JSON output via
        `response_schema=GeminiVideoEvaluation` -- same mechanism as
        every other Gemini structured-output call in this codebase (e.g.
        `course_service._generate_quiz`), so there's no hand-rolled JSON
        parsing here.

        `academic_context` -- Backend Folder Structure & Core Database
        Models step addition: this router's own concrete example of
        that step's "inject the User's academic_level into the LLM
        context prompt" architecture requirement. An optional,
        pre-built sentence from `app/services/ai_context.py::
        build_academic_context`, appended as an EXTRA `contents` entry
        -- `_EVALUATION_SYSTEM_PROMPT` above is left completely
        untouched either way (see this module's own docstring on why:
        that prompt is "word for word" from an earlier step's own ask).
        `None` (the default -- also the caller's own behavior when the
        uploading student has set neither `academic_level` nor `age`)
        omits this block from `contents` entirely.
        """
        contents: list[str] = [_EVALUATION_SYSTEM_PROMPT]
        if academic_context is not None:
            contents.append(academic_context)
        contents.append(f"Transcribed student video content:\n\n{transcript}")

        try:
            response = await self._gemini_client().aio.models.generate_content(
                model=settings.gemini_student_video_model,
                contents=contents,
                config=genai_types.GenerateContentConfig(
                    response_mime_type="application/json",
                    response_schema=GeminiVideoEvaluation,
                ),
            )
        except genai_errors.APIError:
            raise
        except Exception as exc:
            raise VideoEvaluationError(str(exc)) from exc

        if not response.text:
            raise VideoEvaluationError("Gemini returned no content for video evaluation")

        try:
            return GeminiVideoEvaluation.model_validate_json(response.text)
        except Exception as exc:
            raise VideoEvaluationError(f"Gemini returned malformed structured output: {exc}") from exc

    # -----------------------------------------------------------------
    # Sub-step 5: summary embedding (async, OpenAI)
    # -----------------------------------------------------------------

    @_retry_openai_call
    async def generate_summary_embedding(self, summary: str) -> list[float]:
        """Embeds Gemini's `summary` field via OpenAI's
        `text-embedding-3-small` (`settings.ai_embedding_model`) -- the
        SAME model `ai_gateway.generate_embedding` (app/services/ai.py)
        already wraps for the public Opportunity catalog. Deliberately
        calls the raw OpenAI SDK directly here rather than importing
        `ai_gateway` -- `ai_gateway.generate_embedding` is a thin
        one-liner with no logic worth sharing beyond the client
        construction this class already has its own copy of (matching
        `voice_coach_gateway`'s "own copy of a shared shape" posture from
        the Core Video Media Processing Pipeline step's
        `recommendation_service._embed_text` for the identical reason:
        module-boundary cleanliness over a cross-service import for ~5
        lines)."""
        client = self._openai_client()
        try:
            async with client:
                response = await client.embeddings.create(model=settings.ai_embedding_model, input=summary)
        except (OpenAIAPIConnectionError, OpenAIAPITimeoutError, OpenAIRateLimitError):
            raise
        except Exception as exc:
            raise EmbeddingGenerationError(str(exc)) from exc

        if not response.data:
            raise EmbeddingGenerationError("OpenAI returned no embedding data")
        return response.data[0].embedding


# Module-level singleton -- stateless (each call builds its own client),
# same pattern as `ai_gateway`/`voice_coach_gateway`.
student_video_service = StudentVideoService()


class VideoSearchError(Exception):
    """Raised when embedding the search query fails."""


async def search_student_videos(db: AsyncSession, query: str, limit: int = 10) -> list[StudentVideoSearchResult]:
    """`GET /student/videos/search`'s actual pgvector query -- a
    standalone module-level function (not a `StudentVideoService` method)
    deliberately mirroring `app/services/opportunity_search.py`'s
    `semantic_search`, which this is modeled directly on: same shape
    (embed the query, cosine-similarity search, filter + order on the
    raw expression), same reason for living outside the class it's
    adjacent to (`StudentVideoService`'s five methods are all pure
    external-API wrappers taking plain scalar arguments, no `db:
    AsyncSession` anywhere -- adding one DB-coupled method to that class
    would blur an otherwise clean, consistent boundary).

    Reuses `ai_gateway.generate_embedding` (app/services/ai.py) directly
    for the query embedding, the SAME OpenAI `text-embedding-3-small`
    call `StudentVideoService.generate_summary_embedding` also wraps --
    a small, deliberate duplication (two near-identical one-line OpenAI
    embedding wrappers) rather than reaching from this search function
    into the pipeline-internal service class for an unrelated call site,
    same "module-boundary cleanliness over a cross-service import for a
    few lines" posture already established elsewhere in this codebase
    (e.g. `recommendation_service._embed_text` vs. `memory_service._embed_text`).

    Filters to `status == APPROVED` -- disclosed necessary consistency
    with the public feed endpoint (`GET /student/{student_id}/videos`):
    without this, a `PENDING_AI` or `REJECTED` video's summary/tags could
    leak through search results despite never appearing in the public
    feed, the exact same "public route must not surface unapproved
    content" reasoning already applied throughout this codebase.

    Reuses `settings.search_similarity_threshold` -- valid here
    specifically because `StudentVideo.summary_embedding` is the SAME
    1536-dim OpenAI embedding space as `Opportunity.embedding` (see
    `StudentVideo`'s own column comment), so a threshold already tuned
    for that space carries over correctly rather than needing a second,
    separately-tuned setting.
    """
    try:
        query_embedding = await ai_gateway.generate_embedding(query)
    except Exception as exc:
        logger.exception("Embedding generation failed for student video search query %r", query)
        raise VideoSearchError(str(exc)) from exc

    # Same WHERE-vs-ORDER-BY posture already established for
    # `recommendation_service.get_recommended_courses` and
    # `opportunity_search.semantic_search`: `similarity` (a raw
    # SQLAlchemy expression, not a SELECT-list alias reference) is
    # reused directly in both `.where(...)` and `select(...)` below --
    # each use re-emits the full expression, which is exactly what makes
    # this safe under Postgres's WHERE-can't-see-SELECT-aliases rule.
    distance = StudentVideo.summary_embedding.cosine_distance(query_embedding)
    similarity = 1 - distance

    result = await db.execute(
        select(StudentVideo, similarity.label("similarity_score"))
        .where(
            StudentVideo.status == StudentVideoStatus.APPROVED.value,
            StudentVideo.summary_embedding.is_not(None),
            similarity >= settings.search_similarity_threshold,
        )
        .order_by(distance.asc())
        .limit(limit)
    )

    return [
        StudentVideoSearchResult(
            **StudentVideoOut.model_validate(video).model_dump(),
            similarity_score=round(float(score), 4),
        )
        for video, score in result.all()
    ]


# =============================================================================
# Automated WebVTT Captions Integration -- WebVTT generation
# =============================================================================

# Target display duration per caption cue, per this step's own ask
# ("chunking text into 5-7 second intervals"). Using the midpoint rather
# than either bound: a hard 7.0s ceiling makes the LAST sentence in a
# long segment sometimes get squeezed into a much shorter leftover
# window than its neighbors (character-proportional splitting doesn't
# divide evenly); starting from 6.0 keeps this more visually even in
# practice without meaningfully violating the requested range.
_TARGET_CUE_SECONDS = 6.0

# Matches a Latin `. ! ?` or Arabic `؟` sentence terminator followed by
# whitespace. Arabic's comma (`،`) is deliberately NOT included -- it
# marks a clause, not a sentence boundary, and splitting on it would
# produce cues far shorter than the requested 5-7s target.
_SENTENCE_BOUNDARY_RE = re.compile(r"(?<=[.!?؟])\s+")


def _split_into_sentences(text: str) -> list[str]:
    """Splits `text` on sentence-ending punctuation (Latin and Arabic).
    Falls back to the whole string as a single "sentence" if no
    boundary is found at all (e.g. a short clip with one unpunctuated
    utterance) -- `_chunk_segment` handles that single-sentence case
    without further splitting it."""
    stripped = text.strip()
    if not stripped:
        return []
    return [part.strip() for part in _SENTENCE_BOUNDARY_RE.split(stripped) if part.strip()]


def _split_long_sentence_by_words(sentence: str, target_chunk_count: int) -> list[str]:
    """Splits one unpunctuated (or simply very long) sentence into
    `target_chunk_count` roughly-equal word groups. Used by
    `_chunk_segment` for the one case sentence-level splitting alone
    can't fix: a single run-on sentence with no internal `. ! ? ؟`
    boundary at all, long enough that leaving it as one cue would badly
    violate the requested 5-7s target. Word-boundary splitting (not
    character-boundary) so a cue never cuts a word in half."""
    words = sentence.split()
    if target_chunk_count <= 1 or len(words) <= 1:
        return [sentence]
    words_per_chunk = max(1, math.ceil(len(words) / target_chunk_count))
    chunks = [
        " ".join(words[i : i + words_per_chunk]).strip()
        for i in range(0, len(words), words_per_chunk)
    ]
    return [chunk for chunk in chunks if chunk] or [sentence]


def _chunk_segment(start: float, end: float, text: str) -> list[tuple[float, float, str]]:
    """Splits one `(start, end, text)` window into WebVTT cues targeting
    `_TARGET_CUE_SECONDS` each. Sentences are grouped into a cue,
    accumulating until adding the next sentence would push the cue's
    estimated duration (character-count-proportional share of `end -
    start`) past the target, then flushed. A window shorter than the
    target with only one sentence is returned as a single cue unchanged
    -- no reason to further fragment an already-short segment.

    This is intentionally the SAME function whether `(start, end, text)`
    came from one of Whisper's real per-segment timestamps (the primary
    path, `generate_webvtt_captions`'s `segments` branch) or is a single
    synthetic window spanning the entire video (the legacy flat-text
    fallback, `duration_seconds` branch) -- the algorithm doesn't need to
    know or care which; it only ever reasons about "some text, over some
    known time window."
    """
    window_duration = max(end - start, 0.01)
    sentences = _split_into_sentences(text)
    if not sentences:
        return []
    if len(sentences) == 1 and window_duration <= _TARGET_CUE_SECONDS:
        return [(start, end, sentences[0])]

    # Pre-pass: a single sentence whose OWN estimated share of
    # `window_duration` would already badly overshoot the target (e.g. one
    # long, unpunctuated utterance with no internal `.`/`?`/`؟`) is
    # word-split before the normal sentence-grouping pass below runs --
    # otherwise it would render as one oversized cue lingering on screen
    # far past 5-7s, which no amount of *grouping* logic can fix (grouping
    # only ever makes cues bigger, never smaller). 1.5x the target is the
    # threshold, not 1.0x, so a sentence that's merely a little long
    # isn't needlessly fragmented.
    provisional_total_chars = sum(len(sentence) for sentence in sentences) or 1
    expanded_sentences: list[str] = []
    for sentence in sentences:
        estimated_duration = window_duration * (len(sentence) / provisional_total_chars)
        if estimated_duration > _TARGET_CUE_SECONDS * 1.5:
            chunk_count = max(2, math.ceil(estimated_duration / _TARGET_CUE_SECONDS))
            expanded_sentences.extend(_split_long_sentence_by_words(sentence, chunk_count))
        else:
            expanded_sentences.append(sentence)
    sentences = expanded_sentences

    total_chars = sum(len(sentence) for sentence in sentences) or 1
    cues: list[tuple[float, float, str]] = []
    cursor = start
    buffer_sentences: list[str] = []
    buffer_chars = 0
    buffer_start = start

    def flush(end_time: float) -> None:
        nonlocal buffer_sentences, buffer_chars, buffer_start
        if buffer_sentences:
            cues.append((buffer_start, end_time, " ".join(buffer_sentences)))
        buffer_sentences = []
        buffer_chars = 0

    for sentence in sentences:
        sentence_chars = len(sentence)
        sentence_duration = window_duration * (sentence_chars / total_chars)
        buffered_duration = window_duration * (buffer_chars / total_chars)

        if buffer_sentences and buffered_duration + sentence_duration > _TARGET_CUE_SECONDS:
            flush(cursor)
            buffer_start = cursor

        buffer_sentences.append(sentence)
        buffer_chars += sentence_chars
        cursor = min(cursor + sentence_duration, end)

    flush(end)
    return cues


def _format_vtt_timestamp(total_seconds: float) -> str:
    """Formats seconds as WebVTT's required `HH:MM:SS.mmm` timestamp."""
    clamped = max(0.0, total_seconds)
    whole_seconds = int(clamped)
    milliseconds = round((clamped - whole_seconds) * 1000)
    if milliseconds >= 1000:
        milliseconds -= 1000
        whole_seconds += 1
    hours, remainder = divmod(whole_seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{seconds:02d}.{milliseconds:03d}"


def generate_webvtt_captions(
    segments: list[dict] | None,
    transcript_text: str | None,
    duration_seconds: float | int | None,
) -> str:
    """Builds a strict, valid WebVTT string for one video.

    Two data sources, tried in order:

    1. `segments` -- `StudentVideo.transcript_segments`, Whisper's own
       real per-segment timestamps (the primary, accurate path). Each
       dict is `{"start": float, "end": float, "text": str}`.
    2. `transcript_text` + `duration_seconds` -- the legacy fallback for
       any row transcribed before the Automated WebVTT Captions
       Integration step (segments is `None`/empty). This is exactly the
       ask's own originally-literal algorithm: one synthetic window
       spanning `[0, duration_seconds]`, fed through the same
       `_chunk_segment` proportional-splitting logic as the real-segment
       path above.

    Raises `CaptionsUnavailableError` if NEITHER source has usable data
    (e.g. a video transcribed before this step exists, with no
    `duration_seconds` either) -- the router turns that into a 404 rather
    than ever returning an empty-but-200 `.vtt` file.
    """
    windows: list[tuple[float, float, str]]

    if segments:
        windows = [
            (float(segment["start"]), float(segment["end"]), str(segment["text"]))
            for segment in segments
            if str(segment.get("text", "")).strip()
        ]
    elif transcript_text and duration_seconds and float(duration_seconds) > 0:
        windows = [(0.0, float(duration_seconds), transcript_text)]
    else:
        windows = []

    if not windows:
        raise CaptionsUnavailableError("No transcript data available to build captions from")

    cues: list[tuple[float, float, str]] = []
    for start, end, text in windows:
        cues.extend(_chunk_segment(start, end, text))

    if not cues:
        raise CaptionsUnavailableError("Transcript contained no usable text")

    lines = ["WEBVTT", ""]
    for start, end, text in cues:
        lines.append(f"{_format_vtt_timestamp(start)} --> {_format_vtt_timestamp(end)}")
        lines.append(text)
        lines.append("")

    return "\n".join(lines).rstrip("\n") + "\n"
