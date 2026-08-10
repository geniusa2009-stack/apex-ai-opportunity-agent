import logging
from uuid import UUID

from google import genai
from google.genai import errors as genai_errors
from google.genai import types as genai_types
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from app.core.config import settings
from app.models.courses import Course, UserEnrollment
from app.models.student_memory import StudentMemory
from app.services import course_service

logger = logging.getLogger(__name__)

# Same retry posture as every other Gemini-backed service in this codebase.
_retry_gemini_call = retry(
    reraise=True,
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=20),
    retry=retry_if_exception_type(genai_errors.APIError),
)

# Safety cap on how many `StudentMemory` rows contribute to a user's
# averaged interest vector -- NOT a "most recent N" recency window
# (disclosed: `StudentMemory` has no `created_at`/timestamp column at
# all -- see that model's own docstring -- so there is no ordering
# signal available to select "recent" memories by in the first place;
# this just bounds the query for a hypothetical student with an
# unusually large memory history).
INTEREST_VECTOR_MEMORY_LIMIT = 200


class CourseEmbeddingError(Exception):
    """Raised when embedding a course's title+description via Gemini
    fails. Caught by `course_service.create_course` (best-effort --
    course creation itself must not hard-fail over this)."""


class RecommendationQueryError(Exception):
    """Raised when the pgvector-backed recommendation query itself
    fails (as opposed to succeeding with zero results)."""


def _client() -> genai.Client:
    # Same "fresh client per call" posture as every other Gemini-backed
    # service in this codebase -- see VisionGateway._client
    # (app/services/vision.py) for the full event-loop-lifetime reasoning.
    return genai.Client(api_key=settings.gemini_api_key)


@_retry_gemini_call
async def _embed_text(text: str, task_type: str) -> list[float]:
    """Own small copy of the same embedding-call shape as
    `memory_service._embed_text`, deliberately NOT imported from that
    module -- `memory_service._embed_text` is a leading-underscore
    private helper, and reaching across a module boundary to call
    another module's private function is worse for this codebase's
    module-boundary cleanliness than duplicating ~10 lines. Uses the
    SAME `settings.gemini_embedding_model` (`text-embedding-004`,
    768-dim) as `memory_service.py` -- not by coincidence: this is what
    guarantees `Course.embedding` and `StudentMemory.embedding` live in
    the exact same comparable vector space (see `Course.embedding`'s
    column docstring in app/models/courses.py).
    """
    response = await _client().aio.models.embed_content(
        model=settings.gemini_embedding_model,
        contents=[text],
        config=genai_types.EmbedContentConfig(task_type=task_type),
    )
    if not response.embeddings or response.embeddings[0].values is None:
        raise CourseEmbeddingError("Gemini returned no embedding values")
    return response.embeddings[0].values


def _average_vectors(vectors: list[list[float]]) -> list[float]:
    """Plain-Python elementwise mean -- deliberately not a `numpy`
    dependency (not currently in requirements.txt) for one small,
    infrequent aggregation over at most `INTEREST_VECTOR_MEMORY_LIMIT`
    768-dim vectors."""
    dim = len(vectors[0])
    sums = [0.0] * dim
    for vector in vectors:
        for i in range(dim):
            sums[i] += vector[i]
    count = len(vectors)
    return [s / count for s in sums]


async def embed_course(db: AsyncSession, course: Course) -> None:
    """Embeds `course.title` + `course.description` via Gemini and
    persists the result to `course.embedding`. Called from
    `course_service.create_course` (via a local import there -- see that
    function's docstring for why it's local, not module-level) so every
    course has an embedding from the moment it's created, with no
    separate backfill/trigger endpoint needed for the normal path.

    Takes an already-loaded `Course` ORM object (not just an id) since
    its only caller already has one in hand immediately after insert --
    no reason to force a redundant re-fetch.
    """
    text = f"{course.title}\n\n{course.description or ''}".strip()

    try:
        vector = await _embed_text(text, task_type="RETRIEVAL_DOCUMENT")
    except genai_errors.APIError:
        raise
    except CourseEmbeddingError:
        raise
    except Exception as exc:
        raise CourseEmbeddingError(str(exc)) from exc

    course.embedding = vector
    db.add(course)
    await db.commit()
    logger.info("Embedded course %s", course.id)


async def get_user_interest_vector(db: AsyncSession, user_id: UUID) -> list[float] | None:
    """Derives a single "target interest vector" for `user_id`, per the
    ask's own wording: "derived from their history OR enrollment
    embeddings." Rather than picking one and discarding the other,
    both phrases are honored as an ordered fallback ladder:

    1. The average of the user's own `StudentMemory` embeddings ("their
       history") -- richer than enrollment alone, since it reflects
       skills/achievements/interests inferred from the student's ENTIRE
       platform activity (essays, applications, focus sessions, etc. --
       whatever Enterprise Memory has captured), not just what they've
       clicked "enroll" on inside the Academy specifically.
    2. If the student has no `StudentMemory` rows at all yet, the
       average of the `Course.embedding` for courses they're already
       enrolled in ("enrollment embeddings").
    3. If neither signal exists (a genuinely brand-new student with zero
       platform history and zero enrollments), `None` -- the caller
       (`get_recommended_courses`) falls back to a non-personalized
       "newest courses" ordering rather than erroring or returning
       nothing useful.
    """
    memory_result = await db.execute(
        select(StudentMemory.embedding)
        .where(StudentMemory.user_id == user_id)
        .limit(INTEREST_VECTOR_MEMORY_LIMIT)
    )
    memory_vectors = [list(v) for v in memory_result.scalars().all()]
    if memory_vectors:
        return _average_vectors(memory_vectors)

    enrolled_result = await db.execute(
        select(Course.embedding)
        .join(UserEnrollment, UserEnrollment.course_id == Course.id)
        .where(UserEnrollment.user_id == user_id, Course.embedding.isnot(None))
    )
    enrolled_vectors = [list(v) for v in enrolled_result.scalars().all()]
    if enrolled_vectors:
        return _average_vectors(enrolled_vectors)

    return None


async def get_recommended_courses(db: AsyncSession, user_id: UUID, limit: int = 5) -> list[dict]:
    """Returns a ranked list of masked course-detail dicts (via
    `course_service.get_course_detail_by_object` -- "masking premium
    content body details if the user is not enrolled," per the ask's own
    explicit requirement, reusing the exact same logic
    `GET /courses/{id}` already applies rather than a second,
    parallel-but-possibly-divergent masking implementation).

    Excludes courses the student is already enrolled in -- recommending
    what someone is already taking isn't a useful recommendation.

    The pgvector query itself, when a real interest vector is available,
    is exactly what the ask's own "Expand the similarity expression
    fully within the WHERE clause" instruction describes:

        distance_expr = Course.embedding.cosine_distance(interest_vector)
        select(Course, distance_expr.label("distance"))
            .where(..., distance_expr < settings.recommendation_max_distance)
            .order_by(distance_expr.asc())

    `distance_expr` -- the RAW cosine-distance expression -- is written
    out in full a SECOND time inside `.where(...)`, not referenced via
    the `"distance"` label from the SELECT list. This is deliberate, not
    an oversight: Postgres resolves `WHERE` before it resolves the
    SELECT list's own column aliases, so a `WHERE distance > 0.5`
    referencing a `SELECT ... AS distance` alias from the same query
    raises `column "distance" does not exist` -- aliases are only
    visible to clauses evaluated AFTER the select list, i.e. `ORDER BY`/
    `GROUP BY`/`HAVING`. `.order_by(distance_expr.asc())` below could
    equally have referenced the `"distance"` label instead (SQLAlchemy
    and Postgres both allow either form there) -- the raw expression is
    used in both places here simply for consistency within this one
    function, not because ORDER BY requires it.

    Falls back to a non-personalized "newest courses" list (no
    `distance`/`relevance_score` -- both `None`) when
    `get_user_interest_vector` returns `None` (see that function's
    docstring for when that happens) -- a brand-new student still gets a
    useful response instead of an empty one or an error.
    """
    interest_vector = await get_user_interest_vector(db, user_id)

    enrolled_result = await db.execute(
        select(UserEnrollment.course_id).where(UserEnrollment.user_id == user_id)
    )
    enrolled_course_ids = list(enrolled_result.scalars().all())

    if interest_vector is None:
        conditions = []
        if enrolled_course_ids:
            conditions.append(Course.id.notin_(enrolled_course_ids))

        fallback_stmt = select(Course).order_by(Course.created_at.desc()).limit(limit)
        if conditions:
            fallback_stmt = fallback_stmt.where(*conditions)

        result = await db.execute(fallback_stmt)
        courses = list(result.scalars().all())

        recommendations = []
        for course in courses:
            detail = await course_service.get_course_detail_by_object(db, user_id, course)
            detail["cosine_distance"] = None
            detail["relevance_score"] = None
            recommendations.append(detail)
        return recommendations

    distance_expr = Course.embedding.cosine_distance(interest_vector)

    conditions = [Course.embedding.isnot(None), distance_expr < settings.recommendation_max_distance]
    if enrolled_course_ids:
        conditions.append(Course.id.notin_(enrolled_course_ids))

    stmt = (
        select(Course, distance_expr.label("distance"))
        .where(*conditions)
        .order_by(distance_expr.asc())
        .limit(limit)
    )

    try:
        result = await db.execute(stmt)
        rows = result.all()
    except Exception as exc:
        logger.exception("Recommendation query failed for user %s", user_id)
        raise RecommendationQueryError(str(exc)) from exc

    recommendations = []
    for course, distance in rows:
        detail = await course_service.get_course_detail_by_object(db, user_id, course)
        distance_value = float(distance)
        detail["cosine_distance"] = distance_value
        # pgvector's `<=>` returns `1 - cosine_similarity` for normalized
        # comparison, so `1 - distance` IS the cosine similarity -- a
        # well-defined transform, not an arbitrary heuristic score.
        detail["relevance_score"] = round(1.0 - distance_value, 4)
        recommendations.append(detail)
    return recommendations
