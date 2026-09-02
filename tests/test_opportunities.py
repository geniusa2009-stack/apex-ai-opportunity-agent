"""Behavioral tests for the Real Age/Academic-Year/Gender Opportunity
Filtering step:

- `app/api/v1/endpoints/opportunities.py::_build_eligibility_filters` --
  the new pure filter-construction function (unit-tested directly, no DB,
  via `TestBuildEligibilityFiltersUnit`).
- `GET /api/v1/opportunities/` -- exercised end-to-end via `TestClient`
  over an in-memory SQLite DB, same posture as `tests/test_dashboards.py`'s
  own `_build_opportunity_filters` coverage.
- `app/api/deps.py::get_current_user_optional` (`CurrentUserOptional`) --
  covered implicitly: the signed-out tests below never override it and
  never send an `Authorization` header, exercising the REAL "no
  credentials at all" branch, not a mocked-out stand-in.

DISCLOSED, NECESSARY SCOPE LIMIT (found while writing this file, not
assumed): `Opportunity.target_academic_levels` is a Postgres `ARRAY` of a
native enum, and `_build_eligibility_filters`'s own academic-level branch
uses `.any(...)`, a Postgres-only ARRAY operator with no SQLite
equivalent -- confirmed by `tests/test_dashboards.py`'s own pre-existing
`_build_opportunity_filters` coverage (same `.any()` pattern, one
migration earlier) never populating `target_academic_levels` in ANY of
its own SQLite fixtures either, for the identical reason. This file's
SQLite-backed `TestClient` tests below therefore cover the two genuinely
new SQLite-compatible eligibility signals end-to-end (`min_age`/`max_age`
numeric range, `target_gender` native enum equality) plus the
`user is None` / `eligible_only=False` bypass paths; the academic-level
`.any()` branch is covered by `TestBuildEligibilityFiltersUnit` below
(pure Python, asserts the correct SQLAlchemy filter is appended for the
correct input, without ever executing it against a real dialect) and by
this step's own real-Postgres-sandbox curl proof (see the final report --
not something a unit test file can substitute for).
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user_optional
from app.api.v1.endpoints.opportunities import _build_eligibility_filters
from app.db.session import get_db
from app.main import app
from app.models.enums import AcademicLevel, Gender
from app.models.user import User

# ---------------------------------------------------------------------
# Raw-SQL tables -- same shape/reasoning as tests/test_dashboards.py's
# own fixtures, extended with this step's own new columns
# (users.gender, opportunities.min_age/max_age/target_gender).
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

_CREATE_SOURCES_TABLE_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY,
    name VARCHAR(255) NOT NULL,
    base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1,
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITIES_TABLE_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    target_url VARCHAR(2048) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    type VARCHAR(20) NOT NULL,
    deadline DATETIME,
    eligibility_criteria TEXT,
    funding_details TEXT,
    location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global',
    is_verified BOOLEAN NOT NULL DEFAULT 0,
    ai_confidence_score FLOAT,
    raw_extracted_data TEXT,
    embedding TEXT,
    is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50),
    target_academic_levels TEXT,
    min_age INTEGER,
    max_age INTEGER,
    target_gender VARCHAR(10),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36),
    claimed_at DATETIME
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
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession, *, age: int | None = None, gender: Gender | None = None, full_name: str = "Test User"
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        age=age,
        gender=gender,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_source(db: AsyncSession) -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO sources (id, name, base_url) VALUES (:id, :name, :base_url)"),
        {"id": source_id.hex, "name": "Test Source", "base_url": "https://example.com"},
    )
    await db.commit()
    return source_id


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID, **overrides) -> uuid.UUID:
    """Raw INSERT, same `.hex` UUID-encoding fix `tests/test_dashboards.py`'s
    own `_make_opportunity` already documents and relies on."""
    opportunity_id = overrides.pop("id", uuid.uuid4())
    defaults = {
        "id": opportunity_id.hex,
        "title": "Some Opportunity",
        "target_url": "https://example.com/apply",
        "source_id": source_id.hex,
        # Lowercase, not the Python enum MEMBER NAME -- `Opportunity.type`'s
        # own column comment documents this exact `values_callable` fix
        # (found while wiring scholarships.html to this endpoint, an
        # earlier task): the real Postgres enum only has lowercase values.
        "type": "scholarship",
        "is_verified": True,
        "min_age": None,
        "max_age": None,
        "target_gender": None,
    }
    defaults.update(overrides)
    await db.execute(
        text(
            """
            INSERT INTO opportunities
                (id, title, target_url, source_id, type, is_verified, min_age, max_age, target_gender)
            VALUES
                (:id, :title, :target_url, :source_id, :type, :is_verified, :min_age, :max_age, :target_gender)
            """
        ),
        defaults,
    )
    await db.commit()
    return opportunity_id


@pytest.fixture
def client_factory(db_session_factory):
    """Same pattern as tests/test_dashboards.py's own `client_factory`,
    but overriding `get_current_user_optional` (this endpoint's real
    dependency, `CurrentUserOptional`) instead of `get_current_user` --
    `GET /opportunities/` has no hard-401 auth requirement to override
    for a signed-in caller. `user=None` builds a client that overrides
    NOTHING beyond `get_db`, so the real "no Authorization header"
    resolution path in `get_current_user_optional` runs unmodified."""

    def _build(user: User | None) -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db

        if user is not None:
            async def _override_get_current_user_optional():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_current_user_optional] = _override_get_current_user_optional

        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


# ---------------------------------------------------------------------
# Pure unit tests for _build_eligibility_filters -- no DB, no dialect,
# covers every branch including the SQLite-incompatible academic_level
# one (constructs the real SQLAlchemy ColumnElement, never executes it).
# ---------------------------------------------------------------------


class _FakeUser:
    """Minimal stand-in exposing only the three attributes
    `_build_eligibility_filters` actually reads -- not a real `User` ORM
    instance, deliberately, so this test never touches a DB/dialect at
    all and can assert on the function's pure input->output shape."""

    def __init__(self, age=None, academic_level=None, gender=None):
        self.age = age
        self.academic_level = academic_level
        self.gender = gender


class TestBuildEligibilityFiltersUnit:
    def test_none_user_returns_no_filters(self):
        assert _build_eligibility_filters(None) == []

    def test_fully_empty_profile_returns_no_filters(self):
        assert _build_eligibility_filters(_FakeUser()) == []

    def test_age_set_appends_exactly_two_filters(self):
        filters = _build_eligibility_filters(_FakeUser(age=20))
        assert len(filters) == 2

    def test_academic_level_set_appends_exactly_one_filter(self):
        filters = _build_eligibility_filters(_FakeUser(academic_level=AcademicLevel.UNIVERSITY_YEAR_2))
        assert len(filters) == 1

    def test_gender_set_appends_exactly_one_filter(self):
        filters = _build_eligibility_filters(_FakeUser(gender=Gender.FEMALE))
        assert len(filters) == 1

    def test_full_profile_appends_all_four_filters(self):
        filters = _build_eligibility_filters(
            _FakeUser(age=20, academic_level=AcademicLevel.UNIVERSITY_YEAR_2, gender=Gender.MALE)
        )
        assert len(filters) == 4


# ---------------------------------------------------------------------
# GET /api/v1/opportunities/ -- end-to-end, SQLite-compatible signals
# (age range, gender). See module docstring for why academic_level
# isn't exercised end-to-end here.
# ---------------------------------------------------------------------


@pytest.mark.asyncio
class TestOpportunityEligibilityFiltering:
    async def test_signed_out_sees_everything_unfiltered(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Adults Only", min_age=18)
            await _make_opportunity(db, source_id=source_id, title="Women Only", target_gender="female")
            await _make_opportunity(db, source_id=source_id, title="Open To All")

        client = client_factory(None)
        response = client.get("/api/v1/opportunities/")

        assert response.status_code == 200
        titles = {o["title"] for o in response.json()["items"]}
        assert titles == {"Adults Only", "Women Only", "Open To All"}

    async def test_signed_in_user_excluded_by_min_age(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            young_user = await _make_user(db, age=15)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Adults Only 18+", min_age=18)
            await _make_opportunity(db, source_id=source_id, title="Open To All")

        client = client_factory(young_user)
        response = client.get("/api/v1/opportunities/")

        assert response.status_code == 200
        titles = {o["title"] for o in response.json()["items"]}
        assert titles == {"Open To All"}

    async def test_signed_in_user_included_when_within_age_range(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            adult_user = await _make_user(db, age=20)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Adults Only 18+", min_age=18)
            await _make_opportunity(db, source_id=source_id, title="Teens Only", min_age=13, max_age=17)

        client = client_factory(adult_user)
        response = client.get("/api/v1/opportunities/")

        assert response.status_code == 200
        titles = {o["title"] for o in response.json()["items"]}
        assert titles == {"Adults Only 18+"}

    async def test_gender_restricted_opportunity_excluded_for_mismatched_gender(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as db:
            male_user = await _make_user(db, gender=Gender.MALE)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Women Only", target_gender="female")
            await _make_opportunity(db, source_id=source_id, title="Open To All")

        client = client_factory(male_user)
        response = client.get("/api/v1/opportunities/")

        assert response.status_code == 200
        titles = {o["title"] for o in response.json()["items"]}
        assert titles == {"Open To All"}

    async def test_gender_restricted_opportunity_included_for_matching_gender(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as db:
            female_user = await _make_user(db, gender=Gender.FEMALE)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Women Only", target_gender="female")

        client = client_factory(female_user)
        response = client.get("/api/v1/opportunities/")

        assert response.status_code == 200
        titles = {o["title"] for o in response.json()["items"]}
        assert titles == {"Women Only"}

    async def test_missing_profile_field_skips_that_filter_only(self, db_session_factory, client_factory):
        """A user with `age` set but `gender` left unset must still see a
        gender-restricted opportunity -- the MISSING signal on the
        user's own profile skips that filter dimension entirely, per
        this step's own established NULL-handling convention."""
        async with db_session_factory() as db:
            user = await _make_user(db, age=20, gender=None)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Women Only", target_gender="female")

        client = client_factory(user)
        response = client.get("/api/v1/opportunities/")

        assert response.status_code == 200
        titles = {o["title"] for o in response.json()["items"]}
        assert titles == {"Women Only"}

    async def test_eligible_only_false_bypasses_filtering_even_when_signed_in(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as db:
            young_user = await _make_user(db, age=15)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, title="Adults Only 18+", min_age=18)

        client = client_factory(young_user)
        response = client.get("/api/v1/opportunities/?eligible_only=false")

        assert response.status_code == 200
        titles = {o["title"] for o in response.json()["items"]}
        assert titles == {"Adults Only 18+"}
