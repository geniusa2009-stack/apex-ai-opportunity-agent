import logging

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser
from app.models.user import User
from app.services.autofill_agent import (
    AutofillSchema,
    autofill_agent_gateway,
    format_profile_text,
)

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/autofill-profile", tags=["autofill"])


def _build_profile_fields(user: User) -> dict:
    return {
        "full_name": user.full_name,
        "email": user.email,
        "phone": user.phone,
        "degree_level": user.degree_level,
        "major": user.major,
        "gpa": str(user.gpa) if user.gpa is not None else None,
        "graduation_year": str(user.graduation_year) if user.graduation_year is not None else None,
        "skills": ", ".join(user.skills) if user.skills else None,
    }


# Path is exactly "/autofill-profile" -- same `@router.get("")` +
# `prefix="/autofill-profile"` pattern (GET, not POST, per the literal
# ask) used elsewhere in this codebase for single-purpose endpoints.
@router.get("", response_model=AutofillSchema)
async def get_autofill_profile(current_user: CurrentUser) -> AutofillSchema:
    profile_fields = _build_profile_fields(current_user)
    student_profile_text = format_profile_text(profile_fields)

    if not student_profile_text.strip():
        # The user hasn't filled in any profile fields yet (via
        # PATCH /users/me) -- there's nothing to structure. A 422 here
        # is more honest than sending an empty prompt to Gemini and
        # getting back an all-null schema anyway.
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="No profile information is set yet. Update your profile "
            "via PATCH /api/v1/users/me first.",
        )

    try:
        return await autofill_agent_gateway.generate_autofill_schema(student_profile_text)
    except Exception:
        logger.exception("Autofill schema generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Autofill schema generation is temporarily unavailable.",
        )
