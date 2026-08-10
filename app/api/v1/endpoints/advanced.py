import logging
from datetime import datetime, timezone

from fastapi import APIRouter, File, HTTPException, UploadFile, status
from pydantic import BaseModel

from app.api.deps import CurrentUser
from app.services.vision import ExtractedDocumentSchema, vision_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/advanced", tags=["advanced"])

_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


class ExtractedDocumentOut(BaseModel):
    filename: str | None
    extracted_at: datetime
    data: ExtractedDocumentSchema


@router.post("/extract-document", response_model=ExtractedDocumentOut)
async def extract_document(
    current_user: CurrentUser,
    file: UploadFile = File(
        ..., description="An image of a transcript, certificate, or similar document."
    ),
) -> ExtractedDocumentOut:
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type {file.content_type!r}. "
                f"Accepted: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}."
            ),
        )

    # Read-then-check rather than a streaming size guard -- fine at this
    # bootstrapping stage given the 10MB cap and Starlette's UploadFile
    # spooling large uploads to disk past ~1MB rather than holding them
    # fully in memory. Worth revisiting with a proper streaming length
    # check before this endpoint handles real, untrusted user traffic.
    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(image_bytes) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large -- max {_MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
        )

    try:
        extracted = await vision_gateway.extract_document(image_bytes, file.content_type)
    except Exception:
        logger.exception(
            "Document extraction failed for user %s (file=%r)", current_user.id, file.filename
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document extraction is temporarily unavailable.",
        )

    return ExtractedDocumentOut(
        filename=file.filename,
        extracted_at=datetime.now(timezone.utc),
        data=extracted,
    )
