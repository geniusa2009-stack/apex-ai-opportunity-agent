import logging

import pypdf.errors
from fastapi import APIRouter, File, HTTPException, UploadFile, status
from fastapi.responses import Response

from app.api.deps import CurrentUser
from app.models.user import User
from app.services.pdf_filler import PdfHasNoFormFieldsError, pdf_filler_gateway

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /pdf/fill") -- `prefix` carries the
# first segment, `@router.post("/fill")` the second, same multi-segment
# approach as multi_agent_interview.py's "/multi-agent/chat".
router = APIRouter(prefix="/pdf", tags=["pdf-filler"])

# Same allowed-set + size-guard pattern as document_checker.py's upload
# (Advanced Feature 4) / advanced.py's (Step 3.4), adapted for PDFs
# specifically instead of document images.
_ALLOWED_CONTENT_TYPES = {"application/pdf"}
_MAX_FILE_SIZE_BYTES = 15 * 1024 * 1024  # 15 MB


def _build_student_profile(user: User) -> dict:
    # Every currently-tracked profile field, not just the four Legendary
    # Feature 3 needed -- a real application PDF could plausibly ask for
    # any of these (name, contact info, academic details, skills), so
    # this endpoint casts a wider net than any single prior feature did.
    return {
        "Full name": user.full_name,
        "Email": user.email,
        "Phone": user.phone,
        "Degree level": user.degree_level,
        "Major": user.major,
        "GPA": user.gpa,
        "Graduation year": user.graduation_year,
        "Skills": ", ".join(user.skills) if user.skills else None,
        "Target country": user.target_country,
    }


@router.post("/fill")
async def fill_pdf(
    current_user: CurrentUser,
    file: UploadFile = File(..., description="A blank fillable PDF form (AcroForm)."),
) -> Response:
    if file.content_type not in _ALLOWED_CONTENT_TYPES:
        raise HTTPException(
            status_code=status.HTTP_415_UNSUPPORTED_MEDIA_TYPE,
            detail=f"Unsupported file type {file.content_type!r}. Accepted: application/pdf.",
        )

    pdf_bytes = await file.read()
    if not pdf_bytes:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Uploaded file is empty.")
    if len(pdf_bytes) > _MAX_FILE_SIZE_BYTES:
        raise HTTPException(
            status_code=status.HTTP_413_REQUEST_ENTITY_TOO_LARGE,
            detail=f"File too large -- max {_MAX_FILE_SIZE_BYTES // (1024 * 1024)}MB.",
        )

    student_profile = _build_student_profile(current_user)
    if not any(student_profile.values()):
        # Same posture as autofill.py/probability.py/scholar_agent.py's
        # all-empty-profile guards.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No profile information is set yet. Update your profile "
            "via PATCH /api/v1/users/me first.",
        )

    try:
        filled_pdf_bytes = await pdf_filler_gateway.fill_pdf_form(pdf_bytes, student_profile)
    except PdfHasNoFormFieldsError as exc:
        # The specific "handle gracefully with an informative error" case
        # the ask called out -- a client-input problem (this PDF isn't
        # fillable), not a server/AI failure.
        raise HTTPException(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc))
    except pypdf.errors.PyPdfError:
        logger.exception(
            "PDF could not be parsed for user %s (file=%r)", current_user.id, file.filename
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Could not read this file as a valid PDF.",
        )
    except Exception:
        logger.exception(
            "PDF form filling failed for user %s (file=%r)", current_user.id, file.filename
        )
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="PDF form filling is temporarily unavailable.",
        )

    return Response(
        content=filled_pdf_bytes,
        media_type="application/pdf",
        headers={"Content-Disposition": 'attachment; filename="filled_form.pdf"'},
    )
