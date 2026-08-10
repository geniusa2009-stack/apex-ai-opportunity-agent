import logging

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.services.voice_scholar import VoiceSynthesisError, voice_scholar_gateway

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /voice/chat") -- `prefix` carries
# the first segment, `@router.post("/chat")` the second, same
# multi-segment approach as `/multi-agent/chat`.
router = APIRouter(prefix="/voice", tags=["voice-scholar"])

# Same allowed-set + size-guard SHAPE as voice_coach.py's upload (Step
# 3.16) -- duplicated rather than imported across router files, same
# "reuse the shape, not the code, across sibling upload endpoints"
# precedent already used for document_checker.py (Advanced Feature 4)
# reusing advanced.py's (Step 3.4) upload-validation shape.
_ALLOWED_CONTENT_TYPES = {
    "audio/mpeg",
    "audio/mp3",
    "audio/wav",
    "audio/x-wav",
    "audio/webm",
    "audio/mp4",
    "audio/m4a",
    "audio/x-m4a",
    "audio/ogg",
}
_MAX_FILE_SIZE_BYTES = 25 * 1024 * 1024  # 25 MB, matches Groq's free-tier STT limit


class VoiceChatResponse(BaseModel):
    user_transcript: str
    ai_response_text: str
    audio_base64: str


@router.post("/chat", response_model=VoiceChatResponse)
async def voice_chat(
    current_user: CurrentUser,
    file: UploadFile = File(..., description="A voice recording to chat with the academic advisor bot."),
) -> VoiceChatResponse:
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type {file.content_type!r}. "
                f"Accepted: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}."
            ),
        )

    audio_bytes = await file.read()
    if not audio_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(audio_bytes) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large -- max {_MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
        )

    try:
        result = await voice_scholar_gateway.voice_to_voice_chat(audio_bytes)
    except VoiceSynthesisError:
        logger.exception(
            "Voice synthesis (TTS) failed for user %s (file=%r)", current_user.id, file.filename
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Voice reply synthesis is temporarily unavailable.",
        )
    except Exception:
        logger.exception(
            "Voice-to-voice chat failed for user %s (file=%r)", current_user.id, file.filename
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Voice chat is temporarily unavailable.",
        )

    return VoiceChatResponse(**result)
