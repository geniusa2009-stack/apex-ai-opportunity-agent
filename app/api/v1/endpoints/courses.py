import logging
import uuid

from fastapi import APIRouter, HTTPException, status

from app.api.deps import CurrentUser, DbSession, RequireAdmin
from app.schemas.courses import (
    CompleteLessonOut,
    CopilotAskOut,
    CopilotAskRequest,
    CourseCreateRequest,
    CourseDetailOut,
    CourseOut,
    EnrollOut,
    LessonCreateRequest,
    LessonOut,
    ModuleCreateRequest,
    ModuleOut,
    QuizQuestionOut,
)
# UI Merge Pass follow-up ("close the flashcards gap"): `StudyAidOut`
# already existed in `app/schemas/features.py` (built alongside the
# `LessonStudyAid` model) but had no router consumer until now -- see
# `course_service.get_or_generate_study_aid`'s own docstring for the
# full disclosure.
from app.schemas.features import StudyAidOut
from app.services import coach_service, course_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/courses", tags=["courses"])


# `RequireAdmin` (app/api/deps.py) gates the disclosed content-authoring
# routes below -- see `course_service.py`'s "Content authoring" section
# docstring for why these routes exist and why they're admin-only. As of
# the Core Video Media Processing Pipeline step, this same dependency is
# also used by `media_router.py`'s admin video-upload route -- moved to
# `app/api/deps.py` specifically so both routers share one copy of this
# security check rather than each maintaining their own.
# ---------------------------------------------------------------------
# Content authoring -- disclosed necessary plumbing, admin-only. See
# `course_service.py`'s "Content authoring" section docstring: without
# SOME way to create a `Course`/`CourseModule`/`Lesson`, `GET /courses/`
# would always return an empty catalog and this entire feature would be
# unreachable dead infrastructure.
# ---------------------------------------------------------------------


@router.post("", response_model=CourseOut, status_code=status.HTTP_201_CREATED)
async def create_course(
    payload: CourseCreateRequest, current_user: CurrentUser, db: DbSession, _: RequireAdmin
) -> CourseOut:
    course = await course_service.create_course(
        db,
        payload.title,
        payload.description,
        payload.instructor_name,
        payload.token_unlock_cost,
        payload.thumbnail_url,
    )
    return CourseOut.model_validate(course)


@router.post("/{course_id}/modules", response_model=ModuleOut, status_code=status.HTTP_201_CREATED)
async def create_module(
    course_id: uuid.UUID,
    payload: ModuleCreateRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireAdmin,
) -> ModuleOut:
    try:
        module = await course_service.create_module(db, course_id, payload.title, payload.order_index)
    except course_service.CourseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found") from exc

    return ModuleOut.model_validate(module)


@router.post("/modules/{module_id}/lessons", response_model=LessonOut, status_code=status.HTTP_201_CREATED)
async def create_lesson(
    module_id: uuid.UUID,
    payload: LessonCreateRequest,
    current_user: CurrentUser,
    db: DbSession,
    _: RequireAdmin,
) -> LessonOut:
    try:
        lesson = await course_service.create_lesson(
            db,
            module_id,
            payload.title,
            payload.content_type,
            payload.video_url,
            payload.text_content,
            payload.order_index,
        )
    except course_service.ModuleNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course module not found") from exc

    return LessonOut.model_validate(lesson)


# ---------------------------------------------------------------------
# Student-facing routes -- all 6 named directly in the ask.
# ---------------------------------------------------------------------


@router.get("/", response_model=list[CourseOut])
async def list_courses(current_user: CurrentUser, db: DbSession) -> list[CourseOut]:
    courses = await course_service.list_courses(db)
    return [CourseOut.model_validate(c) for c in courses]


@router.get("/{course_id}", response_model=CourseDetailOut)
async def course_detail(course_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> CourseDetailOut:
    try:
        detail = await course_service.get_course_detail(db, current_user.id, course_id)
    except course_service.CourseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found") from exc

    return CourseDetailOut.model_validate(detail)


@router.post("/{course_id}/enroll", response_model=EnrollOut)
async def enroll(course_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> EnrollOut:
    """Production Token Gating & Enrollment Engine step. The atomic
    enroll-and-deduct logic itself already existed (`course_service.
    enroll_in_course`, built for the earlier LMS/Academy step, with
    `SELECT ... FOR UPDATE`-based race safety on the token balance --
    see that function's own docstring) -- this handler was updated to
    match this step's specific new response contract:

    - Idempotent re-enroll -> `200 OK`, `success=True`, a distinct
      "already enrolled" message (per this step's own explicit
      requirement).
    - Insufficient tokens -> `400 Bad Request` (previously this route
      returned `200` with `enrolled=False` -- a disclosed, deliberate
      behavior change: this step's ask explicitly says "immediately
      raise a 400 Bad Request," which the old bool-return shape never
      surfaced as an actual error status at the HTTP layer).
    - Fresh unlock (free or paid) -> `200 OK`, `success=True`,
      "Course unlocked successfully."
    """
    try:
        result = await course_service.enroll_in_course(db, current_user.id, course_id)
    except course_service.CourseNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Course not found") from exc

    if not result.success:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Insufficient token balance")

    account = await coach_service.get_or_create_accountability(db, current_user.id)

    return EnrollOut(
        success=True,
        message="Already enrolled in this course" if result.already_enrolled else "Course unlocked successfully",
        current_tokens=account.tokens_balance,
    )


@router.post("/lessons/{lesson_id}/complete", response_model=CompleteLessonOut)
async def complete_lesson(lesson_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> CompleteLessonOut:
    try:
        result = await course_service.complete_lesson(db, current_user.id, lesson_id)
    except course_service.LessonNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found") from exc
    except course_service.NotEnrolledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc

    return CompleteLessonOut(**result)


@router.post("/lessons/{lesson_id}/copilot-ask", response_model=CopilotAskOut)
async def copilot_ask(
    lesson_id: uuid.UUID, payload: CopilotAskRequest, current_user: CurrentUser, db: DbSession
) -> CopilotAskOut:
    try:
        answer = await course_service.ask_course_copilot(
            db, current_user.id, lesson_id, payload.student_question
        )
    except course_service.LessonNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found") from exc
    except course_service.NotEnrolledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except course_service.CopilotGenerationError as exc:
        logger.exception("Course copilot call failed for lesson %s", lesson_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Course copilot failed: {exc}"
        ) from exc

    return CopilotAskOut(lesson_id=lesson_id, answer=answer)


@router.get("/lessons/{lesson_id}/quiz", response_model=list[QuizQuestionOut])
async def lesson_quiz(lesson_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> list[QuizQuestionOut]:
    try:
        questions = await course_service.generate_lesson_quiz(db, current_user.id, lesson_id)
    except course_service.LessonNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found") from exc
    except course_service.NotEnrolledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except course_service.QuizGenerationError as exc:
        logger.exception("Quiz generation failed for lesson %s", lesson_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Quiz generation failed: {exc}"
        ) from exc

    return [QuizQuestionOut(**q.model_dump()) for q in questions]


@router.get("/lessons/{lesson_id}/study-aid", response_model=StudyAidOut)
async def lesson_study_aid(lesson_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> StudyAidOut:
    """UI Merge Pass follow-up -- see `course_service.get_or_generate_study_aid`'s
    own docstring for the full disclosure (why this lives here rather
    than a new router, generate-on-first-request vs. an async worker,
    and the video-lesson content-source gap). Same three-way error
    mapping as `lesson_quiz`/`copilot_ask` immediately above, since this
    endpoint shares the exact same enrollment guard and Gemini failure
    mode as both."""
    try:
        study_aid = await course_service.get_or_generate_study_aid(db, current_user.id, lesson_id)
    except course_service.LessonNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Lesson not found") from exc
    except course_service.NotEnrolledError as exc:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail=str(exc)) from exc
    except course_service.StudyAidGenerationError as exc:
        logger.exception("Study aid generation failed for lesson %s", lesson_id)
        raise HTTPException(
            status_code=status.HTTP_502_BAD_GATEWAY, detail=f"Study aid generation failed: {exc}"
        ) from exc

    return StudyAidOut.model_validate(study_aid)
