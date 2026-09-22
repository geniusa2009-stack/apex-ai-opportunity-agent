"""Behavioral tests for the Backend Folder Structure & Core Database
Models step's own re-specification of the Student Dashboard's
personalization concept:

- `AcademicLevel` (`app/models/enums.py`) round-trips correctly as a
  native-Enum-backed `User.academic_level` column.
- `app/services/ai_context.py::build_academic_context` -- the "inject
  the User's academic_level into the LLM context prompt" architecture.
- `app/api/v1/endpoints/dashboards.py::_build_opportunity_filters`'s new
  `academic_level` filter dimension, ADDITIVE alongside the pre-existing
  `age_group` one.
- `student_video_service.evaluate_and_moderate`'s new `academic_context`
  parameter -- the concrete `/videos` (AI grading) call site this step
  wires the architecture into.

DISCLOSED SQLite LIMITATION, same class of gap already established for
`Opportunity.embedding` (pgvector) elsewhere in this codebase:
`Opportunity.target_academic_levels` is a `postgresql.ARRAY` of a native
enum; the `.any()` comparator it uses in `_build_opportunity_filters`
compiles to Postgres's `x = ANY (array_column)`, a construct SQLite's
dialect cannot compile at all. `TestOpportunityFilterCompilation` below
therefore verifies this filter by COMPILING the resulting SQLAlchemy
`Select` against the `postgresql` dialect (never executing it) --
`str(select(...).compile(dialect=postgresql.dialect()))` -- the same
"compile, don't execute" posture this project's own `alembic upgrade
head --sql` verification already uses. `User.academic_level` itself
(a plain scalar column, not an array) has no such limitation and IS
exercised by a real, executed SQLite round-trip below.
"""

import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.dialects import postgresql
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.v1.endpoints.dashboards import _build_opportunity_filters
from app.models.enums import AcademicLevel
from app.models.opportunity import Opportunity
from app.models.user import User
from app.services import ai_context
from app.services.student_video_service import StudentVideoService

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

# `User.bookmarks` is `lazy="selectin"` (app/models/user.py) -- it
# eagerly queries `user_bookmarks` the moment ANY `User` row is
# fetched/refreshed via the ORM, same disclosed reason every other test
# file in this codebase that loads a `User` row also creates this table
# (e.g. `tests/test_dashboards.py`'s own fixture), even though no test
# here ever reads `.bookmarks` directly.
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
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:",
        poolclass=StaticPool,
        connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


class TestAcademicLevelColumnRoundTrip:
    """`User.academic_level` is a real, executed SQLite round-trip (unlike
    `Opportunity.target_academic_levels` -- see module docstring) --
    the underlying column is a plain scalar `VARCHAR`, not an `ARRAY`,
    so no Postgres-only comparator is involved in simply reading/writing
    it."""

    @pytest.mark.asyncio
    async def test_academic_level_persists_and_reads_back_as_the_enum_member(self, db_session_factory):
        user_id = uuid.uuid4()
        async with db_session_factory() as db:
            db.add(
                User(
                    id=user_id,
                    email="student@example.com",
                    academic_level=AcademicLevel.SECONDARY_2,
                    age=16,
                )
            )
            await db.commit()

        async with db_session_factory() as db:
            fetched = await db.get(User, user_id)
            assert fetched is not None
            assert fetched.academic_level is AcademicLevel.SECONDARY_2
            assert fetched.age == 16

    @pytest.mark.asyncio
    async def test_academic_level_and_age_group_coexist_independently(self, db_session_factory):
        """DISCLOSED, NECESSARY coverage: `academic_level`/`age` are
        additive alongside the pre-existing `age_group` field (see
        `AcademicLevel`'s own docstring), not a replacement -- a user
        can have one, the other, both, or neither set independently."""
        user_id = uuid.uuid4()
        async with db_session_factory() as db:
            db.add(
                User(
                    id=user_id,
                    email="student2@example.com",
                    age_group="prep_secondary",
                    academic_level=None,
                    age=None,
                )
            )
            await db.commit()

        async with db_session_factory() as db:
            fetched = await db.get(User, user_id)
            assert fetched.age_group == "prep_secondary"
            assert fetched.academic_level is None
            assert fetched.age is None


class TestBuildAcademicContext:
    """`ai_context.build_academic_context` -- pure function, no DB
    needed. Constructs bare (non-persisted) `User` instances directly."""

    def test_returns_none_when_neither_field_is_set(self):
        user = User(id=uuid.uuid4(), academic_level=None, age=None)
        assert ai_context.build_academic_context(user) is None

    def test_includes_both_facts_when_both_are_set(self):
        user = User(id=uuid.uuid4(), academic_level=AcademicLevel.UNIVERSITY_YEAR_3, age=20)
        context = ai_context.build_academic_context(user)
        assert context is not None
        assert "3rd year University" in context
        assert "age: 20" in context

    def test_includes_only_academic_level_when_age_is_unset(self):
        user = User(id=uuid.uuid4(), academic_level=AcademicLevel.PHD, age=None)
        context = ai_context.build_academic_context(user)
        assert context is not None
        assert "PhD" in context
        assert "age:" not in context

    def test_includes_only_age_when_academic_level_is_unset(self):
        user = User(id=uuid.uuid4(), academic_level=None, age=14)
        context = ai_context.build_academic_context(user)
        assert context is not None
        assert "age: 14" in context
        assert "academic level" not in context


class TestOpportunityFilterCompilation:
    """`_build_opportunity_filters`'s new `academic_level` dimension --
    see module docstring for why this compiles against `postgresql`
    rather than executing against SQLite."""

    def _compiled_sql(self, current_user: User) -> str:
        import datetime as dt

        now = dt.datetime.now(dt.timezone.utc)
        query = select(Opportunity).where(*_build_opportunity_filters(current_user, now))
        return str(query.compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": False}))

    def test_no_academic_level_filter_when_unset(self):
        # `select(Opportunity)` always lists `target_academic_levels` in
        # its SELECT clause (it's a real mapped column) -- so the
        # meaningful assertion here is specifically about the WHERE
        # clause's `= ANY (...)` comparator, not the bare column name's
        # presence anywhere in the compiled string.
        user = User(id=uuid.uuid4(), age_group=None, academic_level=None)
        compiled = self._compiled_sql(user)
        assert "= ANY (" not in compiled

    def test_academic_level_filter_added_when_set(self):
        user = User(id=uuid.uuid4(), age_group=None, academic_level=AcademicLevel.SECONDARY_1)
        compiled = self._compiled_sql(user)
        assert "= ANY (" in compiled
        assert "opportunities.target_academic_levels" in compiled
        # Still includes an IS NULL branch (a broadly-applicable,
        # unrestricted opportunity is still a match) -- see
        # `_build_opportunity_filters`'s own docstring.
        assert "target_academic_levels IS NULL" in compiled

    def test_both_age_group_and_academic_level_filters_combine_when_both_set(self):
        user = User(id=uuid.uuid4(), age_group="secondary", academic_level=AcademicLevel.SECONDARY_1)
        compiled = self._compiled_sql(user)
        assert "opportunities.target_age_group =" in compiled
        assert "= ANY (" in compiled


class TestEvaluateAndModerateAcademicContextInjection:
    """`evaluate_and_moderate`'s new `academic_context` parameter -- the
    concrete `/videos` AI-grading call site named in this step's own
    architecture requirement. Mocks `_gemini_client` (same "no real
    network call" reasoning as `tests/test_student_videos.py`'s own
    module docstring) and asserts on the captured `contents` argument,
    rather than mocking `evaluate_and_moderate` itself (which would
    trivially pass without exercising this new code path at all)."""

    async def _run_with_mocked_gemini(self, monkeypatch, academic_context):
        from app.services import student_video_service as svs_module

        captured: dict = {}

        class _FakeModels:
            async def generate_content(self, **kwargs):
                captured.update(kwargs)

                class _Resp:
                    text = (
                        '{"safety_approved": true, "score": 90, '
                        '"summary": "s", "strengths": ["a", "b"], '
                        '"growth_points": ["c", "d"], "tags": ["x", "y", "z"]}'
                    )

                return _Resp()

        class _FakeAio:
            models = _FakeModels()

        class _FakeGeminiClient:
            aio = _FakeAio()

        service = StudentVideoService()
        monkeypatch.setattr(service, "_gemini_client", lambda: _FakeGeminiClient())

        await service.evaluate_and_moderate("A transcript.", academic_context=academic_context)
        return captured["contents"]

    @pytest.mark.asyncio
    async def test_academic_context_appended_when_provided(self, monkeypatch):
        contents = await self._run_with_mocked_gemini(
            monkeypatch, "Student context: academic level: 2nd year Secondary."
        )
        assert len(contents) == 3
        assert contents[1] == "Student context: academic level: 2nd year Secondary."
        assert "A transcript." in contents[2]

    @pytest.mark.asyncio
    async def test_academic_context_omitted_when_none(self, monkeypatch):
        contents = await self._run_with_mocked_gemini(monkeypatch, None)
        assert len(contents) == 2
        assert "A transcript." in contents[1]
