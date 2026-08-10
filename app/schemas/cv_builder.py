"""Pydantic V2 schemas for `GET /ai/cv-builder` (Hyper-Personalized
Platform step). See `app/api/v1/endpoints/ai_cv_builder.py`'s module
docstring for the full disclosed reasoning on why this is a NEW, distinct
endpoint from the pre-existing `POST /ai-tools/generate/cv`.
"""

from datetime import date, datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.models.enums import AcademicLevel


class CVProfileSection(BaseModel):
    """The static "who this person is" section -- pulled directly off
    `User`'s own profile columns (Step 3.20/3.27/Student Dashboard/Backend
    Folder Structure additions -- see `app/models/user.py`), no
    aggregation involved."""

    full_name: str | None
    email: str | None
    age: int | None
    academic_level: AcademicLevel | None
    degree_level: str | None
    major: str | None
    gpa: float | None
    graduation_year: int | None
    target_country: str | None
    skills: list[str] | None


class CVBadgeEntry(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    name: str
    description: str
    icon_url: str | None
    awarded_at: datetime


class CVCourseEntry(BaseModel):
    """One line of `courses` -- an enrollment, not necessarily a
    completion (`UserEnrollment` itself carries no completion flag; see
    `app/models/courses.py`'s own docstring -- per-LESSON completion is
    tracked by `LessonProgress`, but no "is this whole COURSE finished"
    rollup exists anywhere in this codebase to aggregate from). Disclosed
    scope boundary: this section shows "courses this student is/was
    enrolled in," not "courses this student has finished" -- a future
    pass could compute the latter from `LessonProgress` if a real need
    for it is named."""

    course_id: UUID
    title: str
    enrolled_at: datetime


class CVCertificateEntry(BaseModel):
    course_id: UUID
    course_title: str
    issued_at: datetime
    certificate_url: str | None


class CVVideoEntry(BaseModel):
    """One AI-approved UGC video, included as portfolio evidence -- only
    `APPROVED` videos are listed (see the endpoint's own docstring):
    a CV should showcase a student's best AI-graded work, not their
    rejected or still-pending submissions."""

    video_id: UUID
    title: str
    ai_score: int | None


class CVBuilderOut(BaseModel):
    """`GET /ai/cv-builder` response -- "profile metrics, badges, and
    history" per this step's own literal ask, aggregated from FIVE
    already-existing tables (`User`, `GamificationProfile`, `UserBadge`/
    `Badge`, `UserEnrollment`/`Course`, `StudentVideo`, `Certificate`),
    no new data collected."""

    profile: CVProfileSection
    total_xp: int
    current_streak: int
    highest_streak: int
    badges: list[CVBadgeEntry]
    courses: list[CVCourseEntry]
    certificates: list[CVCertificateEntry]
    showcase_videos: list[CVVideoEntry]
    generated_on: date
