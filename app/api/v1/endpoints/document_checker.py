import logging

from fastapi import APIRouter, File, HTTPException, UploadFile, status

from app.api.deps import CurrentUser
from app.services.document_checker import DocumentCheckResult, document_checker_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/check-document", tags=["document-checker"])

# Same allowed-set + size-guard pattern as advanced.py's document upload
# (Step 3.4) -- this endpoint checks the same kind of document images
# (passports, transcripts, translated certificates), just for a
# different purpose (authenticity/acceptability vs. data extraction).
_ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp", "image/heic", "image/heif"}
_MAX_FILE_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB


# Path is exactly "/check-document" -- same `@router.post("")` +
# `prefix="/check-document"` pattern used by every other single-purpose
# POST endpoint since Feature 1.
@router.post("", response_model=DocumentCheckResult)
async def check_document(
    current_user: CurrentUser,
    file: UploadFile = File(
        ..., description="An image of a document to check -- e.g. a passport, transcript, or translated certificate."
    ),
) -> DocumentCheckResult:
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=(
                f"Unsupported file type {file.content_type!r}. "
                f"Accepted: {', '.join(sorted(_ALLOWED_CONTENT_TYPES))}."
            ),
        )

    image_bytes = await file.read()
    if not image_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(image_bytes) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large -- max {_MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
        )

    try:
        result = await document_checker_gateway.analyze_document_image(
            image_bytes, mime_type=file.content_type
        )
    except Exception:
        logger.exception(
            "Document authenticity check failed for user %s (file=%r)",
            current_user.id,
            file.filename,
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Document authenticity checking is temporarily unavailable.",
        )

    return result
