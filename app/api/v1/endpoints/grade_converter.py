import logging

from fastapi import APIRouter, HTTPException, status
from pydantic import BaseModel, Field

from app.api.deps import CurrentUser
from app.services.grade_converter import (
    GradeConversionResult,
    grade_converter_gateway,
)

logger = logging.getLogger(__name__)

# Two path segments in the ask ("POST /grade-converter/convert") --
# `prefix` carries the first segment, `@router.post("/convert")` the
# second, same multi-segment approach as `/competition-scout/match`.
router = APIRouter(prefix="/grade-converter", tags=["grade-converter"])


class ConvertGradesRequest(BaseModel):
    local_grade: str = Field(min_length=1, max_length=100, description='E.g. "95%".')
    local_grading_system: str = Field(
        min_length=1, max_length=255, description='E.g. "Egyptian Thanaweya Amma".'
    )
    target_systems: list[str] = Field(
        min_length=1, description='E.g. ["US GPA", "German Abitur"].'
    )


class ConvertGradesResponse(BaseModel):
    result: GradeConversionResult


# Path is exactly "/grade-converter/convert" -- `prefix="/grade-converter"`
# + `@router.post("/convert")`.
@router.post("/convert", response_model=ConvertGradesResponse)
async def convert_grades(
    payload: ConvertGradesRequest, current_user: CurrentUser
) -> ConvertGradesResponse:
    # No DbSession here -- same posture as every High School feature so
    # far: every input is a literal request-body field.
    try:
        result = await grade_converter_gateway.convert_grades(
            payload.local_grade,
            payload.local_grading_system,
            payload.target_systems,
        )
    except Exception:
        logger.exception("Grade conversion failed for user %s", current_user.id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY,
            detail="Grade conversion is temporarily unavailable.",
        )

    return ConvertGradesResponse(result=result)
