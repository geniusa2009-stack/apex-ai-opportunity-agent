"""Behavioral tests for the Production Token Gating & Enrollment Engine
step: `course_service.enroll_in_course` (the atomic enroll-and-deduct
logic, which already existed from the earlier LMS/Academy step -- see
that function's own docstring) and the `POST /courses/{id}/enroll`
router handler's NEW response contract on top of it (400 on
insufficient funds, the `{success, message, current_tokens}` envelope).

In-memory SQLite, `StaticPool`, same pattern established by
`tests/test_student_videos.py`. `Base.metadata.create_all` is used
directly for THREE of the four tables (`courses`, `user_enrollments`,
`student_accountability`) -- none of them have a `JSONB` column, so
SQLite's DDL compiler (no `visit_JSONB`, per that other file's own
disclosed finding) never gets asked to render one, and `Course.embedding`
(a pgvector `Vector` column) compiles fine on SQLite (same finding
already established in the Core Video Media Processing Pipeline step).

`users` is the one EXCEPTION, discovered while writing this file (not
previously hit by any prior test in this codebase): `User.skills` is a
Postgres `ARRAY(String)` column, and SQLite's DDL compiler has no
`visit_ARRAY` either -- the exact same category of dialect gap as
`JSONB`, just on a different column/type. `users` is therefore created
via raw SQL (`_CREATE_USERS_TABLE_SQL`) for the same reason
`student_videos` was in the other file: the ORM's actual INSERT/SELECT
runtime behavior doesn't care that the DDL was hand-written, only that
the table+columns exist -- and this suite never sets `skills` on any
test user anyway (it's left `NULL`, which binds fine regardless of the
column's declared SQLite type affinity).

Two test classes:
- `TestEnrollInCourseService` -- exercises `course_service.enroll_in_course`
  directly against a real DB session, no HTTP layer at all.
- `TestEnrollRouterHTTP` -- exercises the real FastAPI route via
  `TestClient`, with `get_current_user`/`get_db` dependency-overridden
  (same technique as every other router-level test in this codebase),
  specifically to prove the `400` status code and response envelope are
  what actually cross the wire, not just what the service function
  returns internally.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.courses import Course, UserEnrollment
from app.models.gamification import StudentAccountability
from app.models.user import User
from app.services import course_service

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------
# Shared DB fixture
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
    admin_role VARCHAR(20),
    role_granted_at DATETIME,
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Empty, otherwise-unused in this suite -- exists purely because `User.
# bookmarks` is a `lazy="selectin"` relationship, so any `db.refresh(user)`
# or fresh `SELECT` of a `User` eagerly issues a second query against
# `user_bookmarks`. Without this table present (even empty), that query
# fails with "no such table," discovered while first running this suite.
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


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE_USERS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_USER_BOOKMARKS_TABLE_SQL)
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Course.__table__,
                UserEnrollment.__table__,
                StudentAccountability.__table__,
            ],
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


async def _make_user(db: AsyncSession, tokens_balance: int | None = None) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test Student")
    db.add(user)
    await db.commit()
    await db.refresh(user)

    if tokens_balance is not None:
        account = StudentAccountability(user_id=user.id, tokens_balance=tokens_balance, total_earned_tokens=tokens_balance)
        db.add(account)
        await db.commit()

    return user


async def _make_course(db: AsyncSession, token_unlock_cost: int) -> Course:
    course = Course(
        id=uuid.uuid4(),
        title="Intro to Testing",
        instructor_name="AI Scholar Guide",
        token_unlock_cost=token_unlock_cost,
    )
    db.add(course)
    await db.commit()
    await db.refresh(course)
    return course


# ---------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------


class TestEnrollInCourseService:
    async def test_free_course_enrolls_without_charging(self, db_session):
        user = await _make_user(db_session, tokens_balance=50)
        course = await _make_course(db_session, token_unlock_cost=0)

        result = await course_service.enroll_in_course(db_session, user.id, course.id)

        assert result.success is True
        assert result.already_enrolled is False

        account = await course_service.coach_service.get_or_create_accountability(db_session, user.id)
        assert account.tokens_balance == 50, "free course must not touch the token balance"

    async def test_paid_course_success_deducts_exact_cost(self, db_session):
        user = await _make_user(db_session, tokens_balance=100)
        course = await _make_course(db_session, token_unlock_cost=30)

        result = await course_service.enroll_in_course(db_session, user.id, course.id)

        assert result.success is True
        assert result.already_enrolled is False

        account = await course_service.coach_service.get_or_create_accountability(db_session, user.id)
        assert account.tokens_balance == 70

    async def test_insufficient_tokens_fails_without_charging(self, db_session):
        user = await _make_user(db_session, tokens_balance=10)
        course = await _make_course(db_session, token_unlock_cost=30)

        result = await course_service.enroll_in_course(db_session, user.id, course.id)

        assert result.success is False
        assert result.already_enrolled is False

        account = await course_service.coach_service.get_or_create_accountability(db_session, user.id)
        assert account.tokens_balance == 10, "a failed unlock must never deduct any tokens"

        enrolled = await course_service._is_enrolled(db_session, user.id, course.id)
        assert enrolled is False, "a failed unlock must never create an enrollment row"

    async def test_repeat_enrollment_is_idempotent_and_never_double_charges(self, db_session):
        user = await _make_user(db_session, tokens_balance=100)
        course = await _make_course(db_session, token_unlock_cost=30)

        first = await course_service.enroll_in_course(db_session, user.id, course.id)
        assert first.success is True
        assert first.already_enrolled is False

        second = await course_service.enroll_in_course(db_session, user.id, course.id)
        assert second.success is True
        assert second.already_enrolled is True

        account = await course_service.coach_service.get_or_create_accountability(db_session, user.id)
        assert account.tokens_balance == 70, "a repeat enroll call must not charge a second time"

    async def test_nonexistent_course_raises(self, db_session):
        user = await _make_user(db_session, tokens_balance=100)

        with pytest.raises(course_service.CourseNotFoundError):
            await course_service.enroll_in_course(db_session, user.id, uuid.uuid4())


# ---------------------------------------------------------------------
# Router-level (real HTTP status code / response envelope) tests
# ---------------------------------------------------------------------


class TestEnrollRouterHTTP:
    @pytest.fixture
    def client_factory(self, db_session_factory):
        """Returns a `(TestClient, user)` pair with `get_current_user`/
        `get_db` overridden to use the in-memory DB and a fixed user,
        torn down after each test so overrides never leak between tests.
        """

        async def _build(tokens_balance: int | None):
            async with db_session_factory() as setup_session:
                user = await _make_user(setup_session, tokens_balance=tokens_balance)

            async def _override_get_db():
                async with db_session_factory() as session:
                    yield session

            async def _override_get_current_user():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_db] = _override_get_db
            app.dependency_overrides[get_current_user] = _override_get_current_user

            return TestClient(app), user

        yield _build
        app.dependency_overrides.clear()

    async def test_insufficient_tokens_returns_400(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            course = await _make_course(session, token_unlock_cost=30)

        client, _user = await client_factory(tokens_balance=10)

        response = client.post(f"/api/v1/courses/{course.id}/enroll")

        assert response.status_code == 400
        assert response.json()["detail"] == "Insufficient token balance"

    async def test_successful_paid_unlock_returns_200_envelope(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            course = await _make_course(session, token_unlock_cost=30)

        client, _user = await client_factory(tokens_balance=100)

        response = client.post(f"/api/v1/courses/{course.id}/enroll")

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["message"] == "Course unlocked successfully"
        assert body["current_tokens"] == 70

    async def test_repeat_enroll_returns_200_already_enrolled_message(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            course = await _make_course(session, token_unlock_cost=30)

        client, _user = await client_factory(tokens_balance=100)

        first = client.post(f"/api/v1/courses/{course.id}/enroll")
        assert first.status_code == 200
        assert first.json()["message"] == "Course unlocked successfully"

        second = client.post(f"/api/v1/courses/{course.id}/enroll")
        assert second.status_code == 200
        second_body = second.json()
        assert second_body["success"] is True
        assert second_body["message"] == "Already enrolled in this course"
        assert second_body["current_tokens"] == 70, "the second call must not charge again"
