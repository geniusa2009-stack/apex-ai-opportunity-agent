import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.teacher_guide import (
    TeacherGuideResult,
    teacher_guide_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /teacher-guide/generate") --
# `prefix` carries the first segment, `@router.post("/generate")` the
# second, same multi-segment approach as `/essay-sparker/generate`.
router = APIRouter(prefix="/teacher-guide", tags=["teacher-guide"])


class GenerateTeacherGuideRequest(BaseModel):
    student_name: str = Field(min_length=1, max_length=255)
    target_country: str = Field(min_length=1, max_length=120)
    teacher_subject: str = Field(min_length=1, max_length=120)
    student_strengths: list[str] = Field(min_length=1)


class GenerateTeacherGuideResponse(BaseModel):
    result: TeacherGuideResult


# Path is exactly "/teacher-guide/generate" -- `prefix="/teacher-guide"`
# + `@router.post("/generate")`.
@router.post("/generate", response_model=GenerateTeacherGuideResponse)
async def generate_teacher_guide(
    payload: GenerateTeacherGuideRequest, current_user: CurrentUser
) -> GenerateTeacherGuideResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await teacher_guide_gateway.generate_teacher_guide(
            payload.student_name,
            payload.target_country,
            payload.teacher_subject,
            payload.student_strengths,
        )
    except Exception:
        logger.exception("Teacher guide generation failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Teacher guide generation is temporarily unavailable.",
        )

    return GenerateTeacherGuideResponse(result=result)
