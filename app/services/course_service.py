import logging
import random
from collections import defaultdict
from datetime import datetime, timezone
from typing import NamedTuple
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.courses import Course, CourseModule, Lesson, LessonProgress, UserEnrollment
from app.models.features import LessonStudyAid
from app.services import chat_group_service, coach_service

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# Ask's own literal number, named as a module constant -- same posture as
# `coach_service.DAILY_LOGIN_TOKENS`/`guild_service.STANDARD_COMMENT_TOKENS`.
LESSON_COMPLETION_TOKENS = 10


# ---------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------


class CourseNotFoundError(Exception):
    """Raised when a course doesn't exist."""


class ModuleNotFoundError(Exception):
    """Raised when a course module doesn't exist."""


class LessonNotFoundError(Exception):
    """Raised when a lesson doesn't exist."""


class NotEnrolledError(Exception):
    """Raised when a student tries to complete a lesson, ask the
    copilot, or generate a quiz for a course they haven't enrolled in.

    Disclosed necessary guard: the ask never states this explicitly for
    these three actions, but without it `token_unlock_cost` would be
    purely cosmetic -- a student could skip `POST /courses/{id}/enroll`
    entirely and still complete lessons, farm tokens, and use the AI
    copilot on a "premium" course for free. Routers translate this to
    HTTP 403.
    """


class CopilotGenerationError(Exception):
    """Raised when the Gemini course-copilot call fails."""


class QuizGenerationError(Exception):
    """Raised when the Gemini quiz generation call fails."""


class StudyAidGenerationError(Exception):
    """Raised when the Gemini flashcards/summary generation call fails.

    UI Merge Pass follow-up ("close the flashcards gap"): the
    `LessonStudyAid` model + `StudyAidOut` schema (`app/models/
    features.py`, `app/schemas/features.py`) were built in an earlier
    pass, but no router ever exposed them and nothing ever populated
    `summary_text`/`flashcards` -- that earlier pass's own README
    explicitly disclosed this as "storage model only, not the Gemini
    call that would populate" those fields. This is that Gemini call,
    added here rather than a new file -- see `get_or_generate_study_aid`
    below for why."""


# Short, varied, locally-templated congratulations messages for
# `complete_lesson`. Disclosed choice: NOT a Gemini call, unlike
# `ask_course_copilot`/`generate_lesson_quiz`, which the ask explicitly
# says call Gemini -- `complete_lesson`'s own description doesn't say
# to. Lesson completion is a frequent, low-stakes, high-volume action
# (potentially many times per study session); paying for a network round
# trip to an LLM on every single lesson click would be a poor
# architecture choice for something this routine, unlike the
# once-per-achievement Social Card generation in the prior step. A small
# rotating set of warm, specific-feeling templates gets most of the
# "encouraging" ask's intent without that cost.
_CONGRATS_TEMPLATES = [
    "Lesson complete! You just wrapped up \"{title}\" -- keep that momentum going.",
    "Nicely done finishing \"{title}\"! Every lesson like this adds up fast.",
    "You crushed \"{title}\"! Your future self is going to thank you for this.",
    "Another one in the books -- \"{title}\" is done. On to the next win!",
    "That's a wrap on \"{title}\"! Consistent effort like this is exactly how streaks are built.",
]


def _congrats_message(lesson_title: str) -> str:
    return random.choice(_CONGRATS_TEMPLATES).format(title=lesson_title)


# ---------------------------------------------------------------------
# Gemini structured-output schemas + prompts
# ---------------------------------------------------------------------


class QuizQuestion(BaseModel):
    question: str = Field(description="A single multiple-choice question testing understanding of the lesson content.")
    options: list[str] = Field(
        min_length=4,
        max_length=4,
        description="Exactly 4 answer options, in no particular order.",
    )
    correct_answer_index: int = Field(
        ge=0, le=3, description="Index into `options` (0-3) of the single correct answer."
    )
    explanation: str = Field(
        description="A short, encouraging explanation of why the correct answer is right -- "
        "shown to the student after they see the question, since this quiz is a self-check "
        "study tool with no separate submit/grade step (see the router's disclosed note)."
    )


class _QuizResult(BaseModel):
    # "a 3-question Multiple Choice Quiz" per the ask -- hard-constrained
    # the same way `deadline_alerts._GeneratedMilestoneList` constrains
    # its own explicit count.
    questions: list[QuizQuestion] = Field(min_length=3, max_length=3)


_QUIZ_SYSTEM_PROMPT = """\
You are an expert instructional designer writing a short comprehension check \
for an online course lesson. You will be given the lesson's title and its \
written content.

Write EXACTLY 3 multiple-choice questions that verify a student actually \
understood the material -- grounded strictly in the given lesson content, \
never assuming outside knowledge the lesson didn't cover. Each question needs \
exactly 4 answer options (only one correct), and a short, encouraging \
explanation of why the correct answer is right.

Vary question difficulty slightly and avoid trivially obvious wrong answers --
this should genuinely test comprehension, not just careful reading of the \
question itself.
"""

class StudyAidFlashcard(BaseModel):
    question: str = Field(description="A short, specific question testing recall of one concept from the lesson.")
    answer: str = Field(description="The concise, correct answer to `question`, grounded strictly in the lesson content.")


class _StudyAidResult(BaseModel):
    # UI Merge Pass follow-up. No literal ask names an exact flashcard
    # count for this feature (unlike the quiz's own explicit "3
    # questions") -- 5 is a reasonable, disclosed default: enough to be
    # a genuinely useful study aid, not so many that each card gets
    # trivial/repetitive. `min_length`/`max_length` both set to 5 for the
    # same hard-constraint reasoning `_QuizResult.questions` already uses.
    summary_text: str = Field(
        description="A concise (3-5 sentence) plain-prose summary of the lesson's key points -- "
        "written for a student reviewing before a quiz, not a dry restatement of the title."
    )
    flashcards: list[StudyAidFlashcard] = Field(min_length=5, max_length=5)


_STUDY_AID_SYSTEM_PROMPT = """\
You are an expert study-skills coach creating a review aid for an online \
course lesson. You will be given the lesson's title and its written content.

Produce two things:
1. `summary_text` -- a concise, well-organized 3-5 sentence summary of the \
lesson's key points, written in plain, encouraging prose a student could \
skim right before a quiz.
2. `flashcards` -- EXACTLY 5 question/answer flashcards testing recall of \
the lesson's most important, specific facts or concepts. Questions should be \
short and unambiguous; answers should be concise and strictly grounded in \
the given lesson content, never assuming outside knowledge the lesson \
didn't cover.
"""

_COPILOT_SYSTEM_PROMPT = """\
You are a world-class, extremely patient teacher acting as an AI "Course \
Co-Pilot" for an online lesson. You will be given the lesson's title, its \
written content, and a student's question about it.

Answer the student's question SPECIFICALLY in the context of this lesson's \
material -- ground your explanation in what the lesson actually covers, \
using its own examples and terminology where possible, rather than a \
generic textbook answer. If the student's question goes beyond what the \
lesson covers, gently say so and give your best concise answer anyway.

Be warm, clear, and patient -- never condescending, never assuming the \
student is careless for asking. Break down complex ideas into simple steps. \
Keep your answer focused and readable, not a wall of text.
"""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _generate_quiz(lesson_title: str, lesson_content: str) -> list[QuizQuestion]:
    prompt = f"Lesson title: {lesson_title}\nLesson content:\n{lesson_content or '(no written content on this lesson)'}"
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_courses_model,
            contents=[_QUIZ_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_QuizResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise QuizGenerationError(str(exc)) from exc

    if not response.text:
        raise QuizGenerationError("Gemini returned no content for quiz generation")

    return _QuizResult.model_validate_json(response.text).questions


@_retry_gemini_call
async def _generate_study_aid(lesson_title: str, lesson_content: str) -> _StudyAidResult:
    prompt = f"Lesson title: {lesson_title}\nLesson content:\n{lesson_content or '(no written content on this lesson)'}"
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_courses_model,
            contents=[_STUDY_AID_SYSTEM_PROMPT, prompt],
            config=genai_types.GenerateContentConfig(
                response_mime_type="application/json",
                response_schema=_StudyAidResult,
            ),
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise StudyAidGenerationError(str(exc)) from exc

    if not response.text:
        raise StudyAidGenerationError("Gemini returned no content for study aid generation")

    return _StudyAidResult.model_validate_json(response.text)


@_retry_gemini_call
async def _generate_copilot_answer(lesson_title: str, lesson_content: str, student_question: str) -> str:
    # Plain-text generation (no `response_schema`/JSON mode) -- matches
    # the ask's own literal `-> str` return type for this function.
    # Every other Gemini call in this file (and most across this
    # codebase) forces structured JSON output; this one doesn't need to,
    # since the whole point is one piece of freely-written prose, not
    # data to unpack into typed fields.
    prompt = (
        f"Lesson title: {lesson_title}\n"
        f"Lesson content:\n{lesson_content or '(no written content on this lesson)'}\n\n"
        f"Student's question: {student_question}"
    )
    try:
        response = await _client().aio.models.generate_content(
            model=settings.gemini_courses_model,
            contents=[_COPILOT_SYSTEM_PROMPT, prompt],
        )
    except genai_errors.APIError:
        raise
    except Exception as exc:
        raise CopilotGenerationError(str(exc)) from exc

    if not response.text:
        raise CopilotGenerationError("Gemini returned no content for the course copilot")

    return response.text


# ---------------------------------------------------------------------
# Content authoring (disclosed necessary plumbing)
# ---------------------------------------------------------------------
#
# The ask's "Course Service" section names exactly four functions
# (enroll_in_course, complete_lesson, ask_course_copilot,
# generate_lesson_quiz) and its router section names exactly six
# student-facing routes -- nothing anywhere creates a `Course`,
# `CourseModule`, or `Lesson` in the first place. Without SOME way to
# author content, `GET /courses/` would always return an empty list and
# this entire feature would be unreachable dead infrastructure. Added
# here as disclosed necessary plumbing, gated in the router on
# `current_user.is_superuser` (an admin flag this codebase's `User`
# model already has, from Step 3.1) -- unlike Feature 6's Guilds (where
# any authenticated student can create a guild, a deliberate "no
# gatekeeping" choice), a real LMS's official course catalog is
# naturally instructor/admin-authored content, not open to any student.


async def create_course(
    db: AsyncSession,
    title: str,
    description: str | None,
    instructor_name: str,
    token_unlock_cost: int,
    thumbnail_url: str | None,
) -> Course:
    course = Course(
        title=title,
        description=description,
        instructor_name=instructor_name,
        token_unlock_cost=token_unlock_cost,
        thumbnail_url=thumbnail_url,
    )
    db.add(course)
    await db.commit()
    await db.refresh(course)

    # Disclosed addition (Core Video Media Processing Pipeline &
    # Intelligent Recommendation Engine step): embeds this course's
    # title+description via Gemini and persists it to `course.embedding`
    # so `recommendation_service.get_recommended_courses` has something
    # to rank against the moment the course exists -- no separate
    # "generate embeddings" endpoint or backfill job is needed for the
    # normal path. A LOCAL import (not at module top) is deliberate: it
    # breaks what would otherwise be a circular import --
    # `recommendation_service` imports THIS module (`course_service`) at
    # its own top level to reuse `get_course_detail_by_object` for its
    # masked recommendation results, so `course_service` can't also
    # import `recommendation_service` at module load time without a
    # cycle. Importing it lazily, only inside this one function body,
    # resolves it cleanly since by the time `create_course` actually
    # RUNS, both modules have already finished loading.
    from app.services import recommendation_service

    try:
        await recommendation_service.embed_course(db, course)
    except Exception:
        # Best-effort, per `Course.embedding`'s own column docstring
        # (app/models/courses.py) -- a course whose embedding call
        # failed should still exist and be fully usable, just excluded
        # from recommendation ranking until backfilled. Course creation
        # itself must not hard-fail over a downstream AI call.
        logger.exception("Course embedding failed for newly created course %s -- continuing without it", course.id)

    return course


async def create_module(
    db: AsyncSession, course_id: UUID, title: str, order_index: int | None = None
) -> CourseModule:
    await _get_course_or_404(db, course_id)

    if order_index is None:
        count_result = await db.execute(
            select(func.count()).select_from(CourseModule).where(CourseModule.course_id == course_id)
        )
        order_index = count_result.scalar_one()

    module = CourseModule(course_id=course_id, title=title, order_index=order_index)
    db.add(module)
    await db.commit()
    await db.refresh(module)
    return module


async def create_lesson(
    db: AsyncSession,
    module_id: UUID,
    title: str,
    content_type: str,
    video_url: str | None,
    text_content: str | None,
    order_index: int | None = None,
) -> Lesson:
    await _get_module_or_404(db, module_id)

    if order_index is None:
        count_result = await db.execute(
            select(func.count()).select_from(Lesson).where(Lesson.module_id == module_id)
        )
        order_index = count_result.scalar_one()

    lesson = Lesson(
        module_id=module_id,
        title=title,
        content_type=content_type,
        video_url=video_url,
        text_content=text_content,
        order_index=order_index,
    )
    db.add(lesson)
    await db.commit()
    await db.refresh(lesson)
    return lesson


# ---------------------------------------------------------------------
# Lookups
# ---------------------------------------------------------------------


async def _get_course_or_404(db: AsyncSession, course_id: UUID) -> Course:
    course = await db.get(Course, course_id)
    if course is None:
        raise CourseNotFoundError(str(course_id))
    return course


async def _get_module_or_404(db: AsyncSession, module_id: UUID) -> CourseModule:
    module = await db.get(CourseModule, module_id)
    if module is None:
        raise ModuleNotFoundError(str(module_id))
    return module


async def _get_lesson_or_404(db: AsyncSession, lesson_id: UUID) -> Lesson:
    lesson = await db.get(Lesson, lesson_id)
    if lesson is None:
        raise LessonNotFoundError(str(lesson_id))
    return lesson


async def _is_enrolled(db: AsyncSession, user_id: UUID, course_id: UUID) -> bool:
    result = await db.execute(
        select(UserEnrollment).where(
            UserEnrollment.user_id == user_id, UserEnrollment.course_id == course_id
        )
    )
    return result.scalar_one_or_none() is not None


async def get_lesson_in_course_or_404(db: AsyncSession, course_id: UUID, lesson_id: UUID) -> Lesson:
    """Public lookup for `media_router.py`'s admin video-upload endpoint
    (`POST /admin/courses/{course_id}/lessons/{lesson_id}/upload-video`)
    -- disclosed addition, Core Video Media Processing Pipeline step.

    Verifies `lesson_id` both exists AND actually belongs to the course
    named in the URL's `course_id` segment (via its parent module) --
    without this second check, the two path segments could silently
    mismatch (e.g. a valid lesson id copy-pasted under the wrong
    course's URL) and the video would get attached to a lesson the
    caller didn't intend, with no error at all. Raises
    `LessonNotFoundError` for BOTH failure modes (unknown lesson id, or
    a real lesson that just doesn't belong to this course) -- the router
    maps both to one 404, which is the right response either way: a 403
    for the mismatch case would leak that the lesson id IS valid, just
    under a different course.
    """
    lesson = await _get_lesson_or_404(db, lesson_id)
    module = await _get_module_or_404(db, lesson.module_id)
    if module.course_id != course_id:
        raise LessonNotFoundError(str(lesson_id))
    return lesson


async def _require_enrolled_for_lesson(db: AsyncSession, user_id: UUID, lesson_id: UUID) -> Lesson:
    """Shared guard for `complete_lesson`/`ask_course_copilot`/
    `generate_lesson_quiz` -- all three are scoped to a lesson but the
    thing that's actually gated is enrollment in the lesson's PARENT
    COURSE. Returns the lesson so callers don't have to re-fetch it."""
    lesson = await _get_lesson_or_404(db, lesson_id)
    module = await _get_module_or_404(db, lesson.module_id)
    if not await _is_enrolled(db, user_id, module.course_id):
        raise NotEnrolledError(f"User {user_id} is not enrolled in course {module.course_id}")
    return lesson


# ---------------------------------------------------------------------
# Course listing / detail
# ---------------------------------------------------------------------


async def list_courses(db: AsyncSession) -> list[Course]:
    """Necessary plumbing behind `GET /courses/` -- the ask names the
    endpoint, not a dedicated service method for it."""
    result = await db.execute(select(Course).order_by(Course.created_at.desc()))
    return list(result.scalars().all())


async def get_course_detail(db: AsyncSession, user_id: UUID, course_id: UUID) -> dict:
    """Necessary plumbing behind `GET /courses/{id}` -- the ask names
    the endpoint ("Returns course details: modules, lessons, and
    enrollment/completion progress") but not a dedicated service method.
    Thin wrapper: 404-checked fetch, then delegates the actual
    dict-building to `get_course_detail_by_object` below."""
    course = await _get_course_or_404(db, course_id)
    return await get_course_detail_by_object(db, user_id, course)


async def get_course_detail_by_object(db: AsyncSession, user_id: UUID, course: Course) -> dict:
    """The actual masked course-detail dict builder, factored out of
    `get_course_detail` (disclosed refactor, Core Video Media Processing
    Pipeline & Intelligent Recommendation Engine step) so
    `recommendation_service.get_recommended_courses` can reuse the exact
    same masking logic for each of ITS results too, instead of either
    duplicating it or making one redundant extra DB round-trip per
    recommended course to re-fetch a `Course` object it already has in
    hand from its own ranking query.

    Premium-content masking (disclosed necessary addition, unchanged
    from the original `get_course_detail`): if this course has
    `token_unlock_cost > 0` and the caller is NOT enrolled, each lesson's
    `video_url`/`text_content` are returned as `None` -- only the
    course/module/lesson TITLES and structure are visible, as a
    browsable preview. Free courses (`token_unlock_cost == 0`) always
    show full content, enrolled or not, matching "0 means free/open"
    from the model's own field description.

    Extended (Core Video Media Processing Pipeline step) to mask
    `hls_master_cid`/`video_metadata` under the exact same `show_content`
    gate -- without this, a non-enrolled student could read a premium
    lesson's IPFS CID straight out of this response and stream the video
    directly from a public IPFS gateway, completely bypassing the
    enrollment/token-unlock check `GET /lessons/{id}/stream-manifest`
    itself enforces (see media_router.py). `duration_seconds` and
    `video_processing_status` are deliberately NOT masked -- knowing a
    locked lesson is "an 8-minute video, ready to watch" is harmless
    preview information (arguably useful marketing, like a course
    description) that reveals nothing about the video's actual content.
    """
    is_enrolled = await _is_enrolled(db, user_id, course.id)
    show_content = is_enrolled or course.token_unlock_cost == 0

    modules_result = await db.execute(
        select(CourseModule).where(CourseModule.course_id == course.id).order_by(CourseModule.order_index)
    )
    modules = list(modules_result.scalars().all())
    module_ids = [m.id for m in modules]

    lessons_by_module: dict[UUID, list[Lesson]] = defaultdict(list)
    progress_map: dict[UUID, bool] = {}

    if module_ids:
        lessons_result = await db.execute(
            select(Lesson).where(Lesson.module_id.in_(module_ids)).order_by(Lesson.order_index)
        )
        lessons = list(lessons_result.scalars().all())
        for lesson in lessons:
            lessons_by_module[lesson.module_id].append(lesson)

        lesson_ids = [lesson.id for lesson in lessons]
        if lesson_ids:
            progress_result = await db.execute(
                select(LessonProgress).where(
                    LessonProgress.user_id == user_id, LessonProgress.lesson_id.in_(lesson_ids)
                )
            )
            progress_map = {p.lesson_id: p.is_completed for p in progress_result.scalars().all()}

    module_dicts = []
    for module in modules:
        lesson_dicts = [
            {
                "id": lesson.id,
                "title": lesson.title,
                "content_type": lesson.content_type,
                "video_url": lesson.video_url if show_content else None,
                "text_content": lesson.text_content if show_content else None,
                "order_index": lesson.order_index,
                "is_completed": progress_map.get(lesson.id, False),
                "hls_master_cid": lesson.hls_master_cid if show_content else None,
                # Hybrid Video Thumbnail System addition -- NOT masked
                # under `show_content`, same posture as `duration_seconds`/
                # `video_processing_status` just below (see this
                # function's own docstring): a thumbnail image is
                # harmless preview information, not the premium content
                # itself.
                "thumbnail_cid": lesson.thumbnail_cid,
                "duration_seconds": lesson.duration_seconds,
                "video_processing_status": lesson.video_processing_status,
                "video_metadata": lesson.video_metadata if show_content else None,
            }
            for lesson in lessons_by_module.get(module.id, [])
        ]
        module_dicts.append(
            {"id": module.id, "title": module.title, "order_index": module.order_index, "lessons": lesson_dicts}
        )

    return {
        "id": course.id,
        "title": course.title,
        "description": course.description,
        "instructor_name": course.instructor_name,
        "token_unlock_cost": course.token_unlock_cost,
        "thumbnail_url": course.thumbnail_url,
        # Hybrid Video Thumbnail System addition.
        "thumbnail_cid": course.thumbnail_cid,
        "created_at": course.created_at,
        "is_enrolled": is_enrolled,
        "modules": module_dicts,
    }


# ---------------------------------------------------------------------
# Enrollment / progress / AI features
# ---------------------------------------------------------------------


class EnrollmentResult(NamedTuple):
    """Return type for `enroll_in_course`, as of the Production Token
    Gating & Enrollment Engine step. Disclosed change from the original
    LMS/Academy step's plain `-> bool`: that step's ask only asked for
    "enrolled or not," which a bare `bool` captured fine. THIS step's ask
    additionally wants the router to (a) return a different message for
    "just unlocked" vs. "already enrolled" and (b) raise a real `400` on
    insufficient funds -- neither is derivable from a bare `bool` without
    a second query, so the return type is widened here instead. Verified
    this function has exactly one production call site
    (`app/api/v1/endpoints/courses.py`'s `enroll` handler) before making
    this change, so nothing else needed updating.

    `success=False` implies `already_enrolled=False` -- the only way this
    function fails at all is insufficient tokens on a not-yet-enrolled,
    paid course; every other path succeeds (immediately, idempotently, or
    after a real charge).
    """

    success: bool
    already_enrolled: bool


async def enroll_in_course(db: AsyncSession, user_id: UUID, course_id: UUID) -> EnrollmentResult:
    """Checks if the course requires a token unlock; if so, deducts via
    `coach_service.deduct_tokens` (reused directly, per the ask's own
    literal instruction) before creating the enrollment.

    `EnrollmentResult.success=False` means "insufficient tokens, not
    enrolled"; `success=True` means "enrolled" (either just now, or
    already, idempotently -- calling this twice is a harmless no-op,
    never double-charging) -- see `EnrollmentResult`'s own docstring for
    why the return type was widened from a bare `bool` to this.

    Race handling (disclosed, since there's no natural existing row to
    lock BEFORE the enrollment row itself exists -- unlike
    `social_service.manage_duel`, which locks an already-existing
    `StudyDuel` row): if two concurrent calls both pass the
    not-yet-enrolled check for a paid course, both will deduct tokens,
    but only one wins the `UserEnrollment` insert (its composite PK
    enforces this). The loser's `IntegrityError` is caught and its
    just-deducted tokens are refunded via `coach_service.refund_tokens`
    before returning success (the winner's row is now theirs too) --
    self-healing rather than pre-locking, since the race window is
    narrow and this keeps a student from ever being charged twice for
    landing on the losing side of a race. This directly satisfies this
    step's own "handle potential concurrency race conditions smoothly
    (e.g. a double-clicked unlock button)" requirement -- token deduction
    itself is ALSO safe under true concurrent load independent of this
    race path, via `coach_service.get_or_create_accountability`'s
    `SELECT ... FOR UPDATE` row lock, which serializes two truly
    simultaneous deduct calls for the same user at the database level
    rather than letting them race on a stale in-memory balance read.
    """
    course = await _get_course_or_404(db, course_id)

    if await _is_enrolled(db, user_id, course_id):
        return EnrollmentResult(success=True, already_enrolled=True)

    charged = False
    if course.token_unlock_cost > 0:
        success = await coach_service.deduct_tokens(
            db, user_id, course.token_unlock_cost, f"course_unlock:{course_id}"
        )
        if not success:
            return EnrollmentResult(success=False, already_enrolled=False)
        charged = True

    enrollment = UserEnrollment(user_id=user_id, course_id=course_id)
    db.add(enrollment)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if charged:
            await coach_service.refund_tokens(
                db, user_id, course.token_unlock_cost, f"course_unlock_race:{course_id}"
            )
        logger.info("Enrollment race for user %s / course %s resolved as already-enrolled", user_id, course_id)
        return EnrollmentResult(success=True, already_enrolled=True)

    # AI-Powered Course Cohorts addition -- auto-joins the student into
    # this course's chat cohort room right after a genuinely NEW
    # enrollment. Best-effort: a chat-cohort failure must never turn an
    # otherwise-successful enrollment into a failed request.
    try:
        await chat_group_service.join_course_cohort(db, course_id, course.title, user_id)
    except Exception:
        logger.exception("Course Cohorts: failed to auto-join user %s into course %s cohort", user_id, course_id)

    return EnrollmentResult(success=True, already_enrolled=False)


async def complete_lesson(db: AsyncSession, user_id: UUID, lesson_id: UUID) -> dict:
    """Marks a lesson complete, awards `LESSON_COMPLETION_TOKENS` (10),
    and returns an encouraging message (see `_congrats_message`'s
    docstring for why that's a local template, not a Gemini call).

    Idempotent: re-completing an already-completed lesson returns
    `already_completed=True` with `tokens_earned=0` and does NOT call
    `award_tokens` again -- without this, a student could farm unlimited
    tokens by repeatedly hitting this endpoint on one lesson. Disclosed
    necessary guard, not explicitly stated by the ask.

    Requires the caller to be enrolled in the lesson's parent course
    (see `NotEnrolledError`'s docstring).
    """
    lesson = await _require_enrolled_for_lesson(db, user_id, lesson_id)

    result = await db.execute(
        select(LessonProgress)
        .where(LessonProgress.user_id == user_id, LessonProgress.lesson_id == lesson_id)
        .with_for_update()
    )
    progress = result.scalar_one_or_none()

    if progress is not None and progress.is_completed:
        account = await coach_service.get_or_create_accountability(db, user_id)
        return {
            "lesson_id": lesson_id,
            "already_completed": True,
            "tokens_earned": 0,
            "tokens_balance": account.tokens_balance,
            "congratulations_message": _congrats_message(lesson.title),
        }

    now = datetime.now(timezone.utc)
    if progress is None:
        progress = LessonProgress(user_id=user_id, lesson_id=lesson_id, is_completed=True, completed_at=now)
    else:
        progress.is_completed = True
        progress.completed_at = now
    db.add(progress)

    try:
        await db.commit()
    except IntegrityError:
        # Race: a concurrent call already inserted this exact
        # (user_id, lesson_id) row between our SELECT and INSERT.
        await db.rollback()
        account = await coach_service.get_or_create_accountability(db, user_id)
        return {
            "lesson_id": lesson_id,
            "already_completed": True,
            "tokens_earned": 0,
            "tokens_balance": account.tokens_balance,
            "congratulations_message": _congrats_message(lesson.title),
        }

    account = await coach_service.award_tokens(
        db, user_id, LESSON_COMPLETION_TOKENS, reason=f"lesson_completed:{lesson_id}"
    )

    return {
        "lesson_id": lesson_id,
        "already_completed": False,
        "tokens_earned": LESSON_COMPLETION_TOKENS,
        "tokens_balance": account.tokens_balance,
        "congratulations_message": _congrats_message(lesson.title),
    }


async def ask_course_copilot(db: AsyncSession, user_id: UUID, lesson_id: UUID, student_question: str) -> str:
    """Requires enrollment (see `NotEnrolledError`'s docstring) --
    disclosed addition to the ask's own literal signature, same
    reasoning as `generate_lesson_quiz` below."""
    lesson = await _require_enrolled_for_lesson(db, user_id, lesson_id)
    return await _generate_copilot_answer(lesson.title, lesson.text_content or "", student_question)


async def generate_lesson_quiz(db: AsyncSession, user_id: UUID, lesson_id: UUID) -> list[QuizQuestion]:
    """Disclosed necessary additions to the ask's own literal
    `generate_lesson_quiz(lesson_id: UUID) -> list` signature: `db:
    AsyncSession` (same posture as every DB-writing/reading function in
    this project) and `user_id: UUID`. Every route this ask names is
    under "Authenticated routes," and `complete_lesson`/
    `ask_course_copilot` both require enrollment in the lesson's parent
    course -- it would be a real, disclosed inconsistency to let anyone
    freely generate a premium course's lesson quiz without ever
    enrolling (or paying) for it, when the other two lesson-scoped
    actions can't be reached that way. `user_id` was added so this
    function can apply the same `NotEnrolledError` guard.
    """
    lesson = await _require_enrolled_for_lesson(db, user_id, lesson_id)
    return await _generate_quiz(lesson.title, lesson.text_content or "")


async def get_or_generate_study_aid(db: AsyncSession, user_id: UUID, lesson_id: UUID) -> LessonStudyAid:
    """Closes the disclosed "flashcards gap" from the earlier Hyper-
    Personalized Platform pass: `LessonStudyAid` existed as a storage
    model with no router and no generation call. This is that missing
    piece -- generate-on-first-request, then cache in the DB, exactly
    the pattern `LessonStudyAid.summary_text`/`.flashcards` being
    NULLABLE ("a row's existence doesn't guarantee generation has
    finished yet") already anticipated.

    DISCLOSED PLACEMENT: added to `course_service.py` rather than a new
    `study_aid_service.py` file. `LessonStudyAid` is scoped to exactly
    the same unit of content (`Lesson`) as this file's own
    `ask_course_copilot`/`generate_lesson_quiz`, needs the exact same
    enrollment guard (`_require_enrolled_for_lesson`) and the exact same
    Gemini client/retry plumbing already defined here -- a new file
    would either duplicate all of that or import back into this one,
    with no real separation-of-concerns benefit either way.

    DISCLOSED GENERATE-ON-DEMAND vs. ASYNC WORKER: the model's own
    docstring anticipated "generated asynchronously" (a Celery task).
    This implementation generates SYNCHRONOUSLY on first request instead
    -- the same posture `ask_course_copilot`/`generate_lesson_quiz`
    already use for their own on-demand Gemini calls (both block the
    request until Gemini responds, no task queue). A real async worker
    would need a queued job, a polling/webhook contract for the
    frontend, and a `status` column this table doesn't have (see the
    model's own docstring: "No explicit generation-status column is
    added... a future pass can treat 'both fields NULL' as 'not
    generated yet'") -- meaningfully more infrastructure than this
    feature's one-time-per-lesson cost justifies, and inconsistent with
    the two sibling AI features in this exact file. Every subsequent
    request for the same lesson hits the DB cache below, not Gemini
    again.

    DISCLOSED CONTENT-SOURCE GAP for video lessons: `Lesson.text_content`
    is the only written content this table's `lesson_id` branch has
    access to (course Lessons have no transcript field -- only
    `StudentVideo` does, a separate model this function doesn't touch).
    For a video-only lesson with no `text_content`, Gemini is still
    called with a "(no written content on this lesson)" placeholder
    (same fallback `generate_lesson_quiz` already uses) and will
    generate a summary/flashcards from the lesson TITLE alone -- weaker
    than a real transcript-grounded result, but an honest, disclosed
    degradation rather than silently returning nothing or fabricating
    content from thin air.
    """
    lesson = await _require_enrolled_for_lesson(db, user_id, lesson_id)

    existing = await db.execute(select(LessonStudyAid).where(LessonStudyAid.lesson_id == lesson.id))
    study_aid = existing.scalar_one_or_none()
    if study_aid is not None:
        return study_aid

    generated = await _generate_study_aid(lesson.title, lesson.text_content or "")

    study_aid = LessonStudyAid(
        lesson_id=lesson.id,
        summary_text=generated.summary_text,
        flashcards=[card.model_dump() for card in generated.flashcards],
    )
    db.add(study_aid)
    try:
        await db.commit()
    except IntegrityError:
        # Disclosed race: `lesson_id` is column-`unique=True`. Two
        # concurrent first-requests for the same never-yet-generated
        # lesson could both pass the `scalar_one_or_none() is None` check
        # above before either commits. Rather than surfacing a raw 500 on
        # the loser, roll back and re-read the row the winner just
        # committed -- the caller still gets a valid, real study aid
        # either way, just possibly not the exact one THIS call generated.
        await db.rollback()
        existing_after_race = await db.execute(
            select(LessonStudyAid).where(LessonStudyAid.lesson_id == lesson.id)
        )
        study_aid = existing_after_race.scalar_one()
        return study_aid

    await db.refresh(study_aid)
    return study_aid


async def require_lesson_enrollment(db: AsyncSession, user_id: UUID, lesson_id: UUID) -> Lesson:
    """Public wrapper around `_require_enrolled_for_lesson` -- disclosed
    addition, Core Video Media Processing Pipeline step. Exposed
    specifically for `media_router.py`'s `GET .../stream-manifest`
    endpoint, which needs the exact same "is this user actually enrolled
    in this lesson's parent course" guard as `complete_lesson`/
    `ask_course_copilot`/`generate_lesson_quiz` above -- reusing this one
    private helper via a thin public wrapper rather than duplicating its
    logic in a second module."""
    return await _require_enrolled_for_lesson(db, user_id, lesson_id)
