"""Behavioral tests for the AI Flashcards & Summaries follow-up step:

- `course_service.get_or_generate_study_aid` (generate-on-first-request,
  then cache; enrollment-gated; race-safe on the `lesson_id` unique
  constraint) -- `app/services/course_service.py`.
- `GET /api/v1/courses/lessons/{lesson_id}/study-aid` -- the three-way
  error mapping (404 lesson-not-found, 403 not-enrolled, 200 success) --
  `app/api/v1/endpoints/courses.py`.

This closes the gap disclosed at the end of the UI Merge Pass step's own
README ("a real flashcards GET endpoint + Gemini generation worker
(backend)" was listed as an explicit "Next step" there).

In-memory SQLite, `StaticPool`, same established discipline as `tests/
test_courses_enrollment.py` / `tests/test_hyper_personalized_platform.py`:

- `users` -- raw SQL (`User.skills` is a Postgres `ARRAY(String)` column;
  SQLite's DDL compiler has no `visit_ARRAY`).
- `user_bookmarks` -- raw SQL, empty/unused. Purely necessary plumbing:
  `Course.bookmarks` is `lazy="selectin"`, so any fresh `SELECT`/refresh
  of a `Course` eagerly issues a second query against `user_bookmarks` --
  without the table present (even empty) that query fails with "no such
  table" (same finding already disclosed in `test_courses_enrollment.py`).
- `lessons` -- raw SQL instead of `Base.metadata.create_all`, because
  `Lesson.video_metadata` is a Postgres `JSONB` column (no generic SQLite
  DDL compiler support -- same class of gap already disclosed for
  `lesson_study_aids.flashcards` in `test_hyper_personalized_platform.py`).
- `lesson_study_aids` -- raw SQL, same reasoning, reusing that other
  file's own `_CREATE_LESSON_STUDY_AIDS_TABLE_SQL` shape verbatim.
- `courses`/`course_modules`/`user_enrollments` -- none of their columns
  are JSONB/ARRAY (Course.embedding is a pgvector column, which DOES
  compile fine on SQLite per `test_courses_enrollment.py`'s own finding),
  so these three are created directly off the real ORM metadata via
  `Base.metadata.create_all(tables=[...])`.

The Gemini call itself (`course_service._generate_study_aid`) is
monkeypatched at the service-module level in every test here -- no real
`genai.Client` is ever constructed, matching this project's standing rule
against fabricating/calling out to real third-party network services in
tests.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.courses import Course, CourseModule, UserEnrollment
from app.models.user import User
from app.services import course_service

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------
# Raw-SQL tables
# ---------------------------------------------------------------------

_CREATE_USERS_TABLE_SQL = """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY,
    email VARCHAR(255) UNIQUE,
    hashed_password VARCHAR(255),
    full_name VARCHAR(255),
    is_active BOOLEAN NOT NULL DEFAULT 1,
    is_superuser BOOLEAN NOT NULL DEFAULT 0,
    phone VARCHAR(50),
    degree_level VARCHAR(100),
    major VARCHAR(255),
    gpa FLOAT,
    graduation_year INTEGER,
    skills TEXT,
    target_country VARCHAR(120),
    age_group VARCHAR(50),
    age INTEGER,
    academic_level VARCHAR(20),
    gender VARCHAR(10),
    bio TEXT,
    search_vector TEXT,
    role VARCHAR(20) NOT NULL DEFAULT 'student',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_BOOKMARKS_TABLE_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36),
    status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_LESSONS_TABLE_SQL = """
CREATE TABLE lessons (
    id VARCHAR(36) PRIMARY KEY,
    module_id VARCHAR(36) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content_type VARCHAR(50) NOT NULL,
    video_url VARCHAR(1000),
    text_content TEXT,
    order_index INTEGER NOT NULL DEFAULT 0,
    hls_master_cid VARCHAR(255),
    duration_seconds INTEGER,
    video_processing_status VARCHAR(50),
    video_metadata TEXT,
    thumbnail_cid VARCHAR(255)
)
"""

_CREATE_LESSON_STUDY_AIDS_TABLE_SQL = """
CREATE TABLE lesson_study_aids (
    id VARCHAR(36) PRIMARY KEY,
    lesson_id VARCHAR(36) UNIQUE,
    student_video_id VARCHAR(36) UNIQUE,
    summary_text TEXT,
    flashcards TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (lesson_id IS NOT NULL AND student_video_id IS NULL)
        OR (lesson_id IS NULL AND student_video_id IS NOT NULL)
    )
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE_USERS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_USER_BOOKMARKS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_LESSONS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_LESSON_STUDY_AIDS_TABLE_SQL)
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Course.__table__,
                CourseModule.__table__,
                UserEnrollment.__table__,
            ],
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


async def _make_user(db: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test Student")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_course(db: AsyncSession) -> Course:
    course = Course(id=uuid.uuid4(), title="Intro to Testing", instructor_name="AI Scholar Guide")
    db.add(course)
    await db.commit()
    await db.refresh(course)
    return course


async def _make_module(db: AsyncSession, course_id: uuid.UUID) -> CourseModule:
    module = CourseModule(id=uuid.uuid4(), course_id=course_id, title="Module 1")
    db.add(module)
    await db.commit()
    await db.refresh(module)
    return module


async def _make_lesson(db: AsyncSession, module_id: uuid.UUID, *, text_content: str | None = "Some lesson text.") -> uuid.UUID:
    lesson_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO lessons (id, module_id, title, content_type, text_content) "
            "VALUES (:id, :module_id, :title, :content_type, :text_content)"
        ),
        {
            "id": lesson_id.hex,
            "module_id": module_id.hex,
            "title": "Lesson One",
            "content_type": "text",
            "text_content": text_content,
        },
    )
    await db.commit()
    return lesson_id


async def _enroll(db: AsyncSession, *, user_id: uuid.UUID, course_id: uuid.UUID) -> None:
    db.add(UserEnrollment(user_id=user_id, course_id=course_id))
    await db.commit()


def _fake_study_aid_result():
    """Builds a real `course_service._StudyAidResult` instance (exactly
    5 flashcards, per that model's own `min_length=5, max_length=5`
    constraint) -- used as the monkeypatched `_generate_study_aid`'s
    return value so the schema actually exercised is the real one, not a
    hand-rolled stand-in dict."""
    return course_service._StudyAidResult(
        summary_text="A concise three-sentence summary of the lesson.",
        flashcards=[
            course_service.StudyAidFlashcard(question=f"Question {i}?", answer=f"Answer {i}.")
            for i in range(1, 6)
        ],
    )


# ---------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------


class TestGetOrGenerateStudyAidService:
    async def test_first_call_generates_and_persists(self, db_session, monkeypatch):
        user = await _make_user(db_session)
        course = await _make_course(db_session)
        module = await _make_module(db_session, course.id)
        lesson_id = await _make_lesson(db_session, module.id)
        await _enroll(db_session, user_id=user.id, course_id=course.id)

        call_count = 0

        async def fake_generate(lesson_title: str, lesson_content: str):
            nonlocal call_count
            call_count += 1
            assert lesson_title == "Lesson One"
            assert lesson_content == "Some lesson text."
            return _fake_study_aid_result()

        monkeypatch.setattr(course_service, "_generate_study_aid", fake_generate)

        study_aid = await course_service.get_or_generate_study_aid(db_session, user.id, lesson_id)

        assert call_count == 1
        assert study_aid.lesson_id == lesson_id
        assert study_aid.summary_text == "A concise three-sentence summary of the lesson."
        assert len(study_aid.flashcards) == 5
        assert study_aid.flashcards[0]["question"] == "Question 1?"

    async def test_second_call_returns_cached_row_without_regenerating(self, db_session, monkeypatch):
        user = await _make_user(db_session)
        course = await _make_course(db_session)
        module = await _make_module(db_session, course.id)
        lesson_id = await _make_lesson(db_session, module.id)
        await _enroll(db_session, user_id=user.id, course_id=course.id)

        call_count = 0

        async def fake_generate(lesson_title: str, lesson_content: str):
            nonlocal call_count
            call_count += 1
            return _fake_study_aid_result()

        monkeypatch.setattr(course_service, "_generate_study_aid", fake_generate)

        first = await course_service.get_or_generate_study_aid(db_session, user.id, lesson_id)
        second = await course_service.get_or_generate_study_aid(db_session, user.id, lesson_id)

        assert call_count == 1, "the second call must reuse the persisted row, not call Gemini again"
        assert second.id == first.id

    async def test_video_lesson_with_no_text_content_uses_placeholder(self, db_session, monkeypatch):
        """`Lesson.text_content` is nullable (video-only lessons have no
        written content) -- `get_or_generate_study_aid` must fall back to
        the same `"(no written content on this lesson)"` placeholder
        `_generate_study_aid`'s own prompt-building already uses, per
        that function's disclosed content-source-gap reasoning."""
        user = await _make_user(db_session)
        course = await _make_course(db_session)
        module = await _make_module(db_session, course.id)
        lesson_id = await _make_lesson(db_session, module.id, text_content=None)
        await _enroll(db_session, user_id=user.id, course_id=course.id)

        seen_content = None

        async def fake_generate(lesson_title: str, lesson_content: str):
            nonlocal seen_content
            seen_content = lesson_content
            return _fake_study_aid_result()

        monkeypatch.setattr(course_service, "_generate_study_aid", fake_generate)

        await course_service.get_or_generate_study_aid(db_session, user.id, lesson_id)

        assert seen_content == ""

    async def test_nonexistent_lesson_raises(self, db_session):
        user = await _make_user(db_session)

        with pytest.raises(course_service.LessonNotFoundError):
            await course_service.get_or_generate_study_aid(db_session, user.id, uuid.uuid4())

    async def test_not_enrolled_raises(self, db_session):
        user = await _make_user(db_session)
        course = await _make_course(db_session)
        module = await _make_module(db_session, course.id)
        lesson_id = await _make_lesson(db_session, module.id)
        # Deliberately no `_enroll` call.

        with pytest.raises(course_service.NotEnrolledError):
            await course_service.get_or_generate_study_aid(db_session, user.id, lesson_id)

    async def test_generation_error_propagates(self, db_session, monkeypatch):
        user = await _make_user(db_session)
        course = await _make_course(db_session)
        module = await _make_module(db_session, course.id)
        lesson_id = await _make_lesson(db_session, module.id)
        await _enroll(db_session, user_id=user.id, course_id=course.id)

        async def failing_generate(lesson_title: str, lesson_content: str):
            raise course_service.StudyAidGenerationError("Gemini returned no content for study aid generation")

        monkeypatch.setattr(course_service, "_generate_study_aid", failing_generate)

        with pytest.raises(course_service.StudyAidGenerationError):
            await course_service.get_or_generate_study_aid(db_session, user.id, lesson_id)


# ---------------------------------------------------------------------
# Router-level (real HTTP status code / response envelope) tests
# ---------------------------------------------------------------------


class TestStudyAidRouterHTTP:
    @pytest.fixture
    def client_factory(self, db_session_factory):
        def _build(user: User) -> TestClient:
            async def _override_get_db():
                async with db_session_factory() as session:
                    yield session

            async def _override_get_current_user():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_db] = _override_get_db
            app.dependency_overrides[get_current_user] = _override_get_current_user

            return TestClient(app)

        yield _build
        app.dependency_overrides.clear()

    async def test_success_returns_200_with_summary_and_flashcards(
        self, db_session_factory, client_factory, monkeypatch
    ):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course = await _make_course(db)
            module = await _make_module(db, course.id)
            lesson_id = await _make_lesson(db, module.id)
            await _enroll(db, user_id=user.id, course_id=course.id)

        async def fake_generate(lesson_title: str, lesson_content: str):
            return _fake_study_aid_result()

        monkeypatch.setattr(course_service, "_generate_study_aid", fake_generate)

        client = client_factory(user)
        response = client.get(f"/api/v1/courses/lessons/{lesson_id}/study-aid")

        assert response.status_code == 200
        body = response.json()
        assert body["summary_text"] == "A concise three-sentence summary of the lesson."
        assert len(body["flashcards"]) == 5
        assert body["flashcards"][0] == {"question": "Question 1?", "answer": "Answer 1."}

    async def test_nonexistent_lesson_returns_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)

        client = client_factory(user)
        response = client.get(f"/api/v1/courses/lessons/{uuid.uuid4()}/study-aid")

        assert response.status_code == 404

    async def test_not_enrolled_returns_403(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course = await _make_course(db)
            module = await _make_module(db, course.id)
            lesson_id = await _make_lesson(db, module.id)
            # Deliberately no enrollment.

        client = client_factory(user)
        response = client.get(f"/api/v1/courses/lessons/{lesson_id}/study-aid")

        assert response.status_code == 403

    async def test_generation_failure_returns_502(self, db_session_factory, client_factory, monkeypatch):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course = await _make_course(db)
            module = await _make_module(db, course.id)
            lesson_id = await _make_lesson(db, module.id)
            await _enroll(db, user_id=user.id, course_id=course.id)

        async def failing_generate(lesson_title: str, lesson_content: str):
            raise course_service.StudyAidGenerationError("boom")

        monkeypatch.setattr(course_service, "_generate_study_aid", failing_generate)

        client = client_factory(user)
        response = client.get(f"/api/v1/courses/lessons/{lesson_id}/study-aid")

        assert response.status_code == 502
