"""Hyper-Personalized Platform step: `GET /ai/cv-builder` -- "Aggregates
user profile metrics, badges, and history into a tailored JSON CV."

DISCLOSED DISTINCTION from the pre-existing `POST /ai-tools/generate/cv`
(`app/api/v1/endpoints/ai_tools.py`), confirmed by reading that endpoint's
full implementation before writing this one (task #437's research) rather
than assuming a name collision meant a duplicate:

- `POST /ai-tools/generate/cv` requires an `opportunity_id` in its request
  body and calls Gemini (`ai_gateway`) to author FREE-FORM MARKDOWN prose
  tailored to that one specific opportunity's eligibility criteria --
  fundamentally an opportunity-application document generator.
- `GET /ai/cv-builder` (this endpoint) takes NO opportunity context at
  all, calls NO LLM, and returns STRUCTURED JSON -- a direct, literal
  reading of this step's own description ("Aggregates ... into a tailored
  JSON CV," not "generates CV prose"). "Tailored" here means "populated
  from this specific user's own real data" (their actual XP, badges,
  courses, certificates), not LLM-personalized wording the way the
  existing endpoint's markdown output is.

Both endpoints coexist; neither replaces the other.

Placed in its OWN router file (rather than added to `ai_tools.py`)
because this step's own ask puts it under a literal `/ai` prefix,
distinct from `ai_tools.py`'s existing `/ai-tools` prefix -- two
different path segments, so a new `APIRouter` is needed regardless of
which file the route function lives in; kept in a dedicated file to
avoid conflating this pass's genuinely new, non-LLM aggregation logic
with `ai_tools.py`'s existing Gemini-backed document generators.
"""

import datetime as dt

from fastapi import APIRouter
from sqlalchemy import select

from app.api.deps import CurrentUser, DbSession
from app.models.badges import Badge, GamificationProfile, UserBadge
from app.models.courses import Course, UserEnrollment
from app.models.enums import StudentVideoStatus
from app.models.features import Certificate
from app.models.student_videos import StudentVideo
from app.schemas.cv_builder import (
    CVBadgeEntry,
    CVBuilderOut,
    CVCertificateEntry,
    CVCourseEntry,
    CVProfileSection,
    CVVideoEntry,
)

router = APIRouter(prefix="/ai", tags=["ai-cv-builder"])


@router.get("/cv-builder", response_model=CVBuilderOut)
async def build_cv(current_user: CurrentUser, db: DbSession) -> CVBuilderOut:
    profile = CVProfileSection(
        full_name=current_user.full_name,
        email=current_user.email,
        age=current_user.age,
        academic_level=current_user.academic_level,
        degree_level=current_user.degree_level,
        major=current_user.major,
        gpa=current_user.gpa,
        graduation_year=current_user.graduation_year,
        target_country=current_user.target_country,
        skills=current_user.skills,
    )

    gamification = await db.get(GamificationProfile, current_user.id)
    total_xp = gamification.total_xp if gamification is not None else 0
    current_streak = gamification.current_streak if gamification is not None else 0
    highest_streak = gamification.highest_streak if gamification is not None else 0

    badges_result = await db.execute(
        select(Badge, UserBadge.awarded_at)
        .join(UserBadge, UserBadge.badge_id == Badge.id)
        .where(UserBadge.user_id == current_user.id)
        .order_by(UserBadge.awarded_at.desc())
    )
    badges = [
        CVBadgeEntry(name=badge.name, description=badge.description, icon_url=badge.icon_url, awarded_at=awarded_at)
        for badge, awarded_at in badges_result.all()
    ]

    courses_result = await db.execute(
        select(Course.id, Course.title, UserEnrollment.enrolled_at)
        .join(UserEnrollment, UserEnrollment.course_id == Course.id)
        .where(UserEnrollment.user_id == current_user.id)
        .order_by(UserEnrollment.enrolled_at.desc())
    )
    courses = [
        CVCourseEntry(course_id=course_id, title=title, enrolled_at=enrolled_at)
        for course_id, title, enrolled_at in courses_result.all()
    ]

    certificates_result = await db.execute(
        select(Certificate.course_id, Course.title, Certificate.issued_at, Certificate.certificate_url)
        .join(Course, Course.id == Certificate.course_id)
        .where(Certificate.user_id == current_user.id)
        .order_by(Certificate.issued_at.desc())
    )
    certificates = [
        CVCertificateEntry(course_id=course_id, course_title=title, issued_at=issued_at, certificate_url=url)
        for course_id, title, issued_at, url in certificates_result.all()
    ]

    # Only `APPROVED` videos -- see `CVVideoEntry`'s own docstring: a CV
    # showcases a student's best AI-graded work, not rejected/still-
    # pending submissions.
    videos_result = await db.execute(
        select(StudentVideo.id, StudentVideo.title, StudentVideo.ai_score)
        .where(
            StudentVideo.student_id == current_user.id,
            StudentVideo.status == StudentVideoStatus.APPROVED.value,
        )
        .order_by(StudentVideo.created_at.desc())
    )
    showcase_videos = [
        CVVideoEntry(video_id=video_id, title=title, ai_score=ai_score)
        for video_id, title, ai_score in videos_result.all()
    ]

    return CVBuilderOut(
        profile=profile,
        total_xp=total_xp,
        current_streak=current_streak,
        highest_streak=highest_streak,
        badges=badges,
        courses=courses,
        certificates=certificates,
        showcase_videos=showcase_videos,
        generated_on=dt.datetime.now(dt.timezone.utc).date(),
    )
