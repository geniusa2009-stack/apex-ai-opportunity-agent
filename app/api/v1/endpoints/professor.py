import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.professor import ProfessorEmailDraft, professor_gateway

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/draft-professor-email", tags=["professor"])


class DraftProfessorEmailRequest(BaseModel):
    professor_background: str = Field(
        min_length=10,
        max_length=8000,
        description="The professor's research background -- interests, "
        "recent papers/projects, lab focus, etc.",
    )
    student_profile: str = Field(
        min_length=10,
        max_length=8000,
        description="The student's own background -- relevant coursework, "
        "research experience, skills, goals.",
    )


class DraftProfessorEmailResponse(BaseModel):
    result: ProfessorEmailDraft


# Path is exactly "/draft-professor-email" -- same `@router.post("")` +
# `prefix="/draft-professor-email"` pattern used by every other
# single-purpose POST endpoint since Feature 1.
@router.post("", response_model=DraftProfessorEmailResponse)
async def draft_professor_email(
    payload: DraftProfessorEmailRequest, current_user: CurrentUser
) -> DraftProfessorEmailResponse:
    # No opportunity/DB lookup here -- same precedent as polisher.py
    # (Feature 7): this operates purely on the two text blocks the
    # caller supplies, no opportunity context needed or requested.
    try:
        result = await professor_gateway.draft_outreach_email(
            payload.professor_background, payload.student_profile
        )
    except Exception:
        logger.exception("Professor outreach email drafting failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Professor outreach email drafting is temporarily unavailable.",
        )

    return DraftProfessorEmailResponse(result=result)
