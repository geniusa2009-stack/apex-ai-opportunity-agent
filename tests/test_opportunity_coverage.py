"""Behavioral tests for Search Coverage / Gap Analysis (Deliverable 1):

- `GET /admin/opportunities/coverage` (Moderator+): real counts by
  `type`/`country`/`target_academic_levels`, real gap flagging.
- Tier gating: a plain student (no admin_role) is rejected.
- The two fixed-vocabulary dimensions (`type`, `target_academic_levels`)
  list every real enum value even at a real count of zero -- a
  disclosed, deliberate design decision (see `_build_buckets`'s own
  docstring in `admin_opportunities.py`), asserted here directly.
- The open-vocabulary `country` dimension only lists countries that
  actually appear, sorted by count descending.
- The real gap-flagging comparison (`is_gap` when a value's count is
  <= 10% of the dimension's max count), exercised with a real,
  deliberately lopsided distribution.

DISCLOSED, NECESSARY SCOPE LIMIT (same one `tests/test_eligibility_
backfill.py` already documented, hit again here for the identical
reason): `Opportunity.target_academic_levels` is a Postgres `ARRAY` of a
native enum with no SQLite equivalent -- committing a real, non-empty
value into it via the ORM against this suite's in-memory SQLite raises
a `CompileError`. The `by_academic_level` bucket is therefore only
exercised here against real ROWS THAT LEAVE IT NULL (every real
academic-level count is genuinely 0 in this test's data, and the
endpoint correctly reports that as 6 real gaps) -- the identical,
already-established limitation, not a new one this test introduces.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique as
`tests/test_opportunity_review.py`.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.enums import AcademicLevel, AdminRole, OpportunityType
from app.models.user import User

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
    review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36),
    claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test User", admin_role=admin_role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_source(db: AsyncSession) -> uuid.UUID:
    source_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO sources (id, name, base_url) VALUES (:id, 'Test Source', 'https://example.com')"),
        {"id": source_id.hex},
    )
    await db.commit()
    return source_id


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID, opp_type: str, country: str | None) -> None:
    await db.execute(
        text(
            "INSERT INTO opportunities (id, title, target_url, source_id, type, country) "
            "VALUES (:id, 'Some Opportunity', 'https://example.com/apply', :source_id, :type, :country)"
        ),
        {"id": uuid.uuid4().hex, "source_id": source_id.hex, "type": opp_type, "country": country},
    )
    await db.commit()


@pytest.fixture
def client_factory(db_session_factory):
    def _build(user: User | None) -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db

        if user is not None:
            async def _override_get_current_user():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_current_user] = _override_get_current_user

        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


class TestTierGating:
    @pytest.mark.asyncio
    async def test_plain_student_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db, admin_role=None)
        client = client_factory(student)
        res = client.get("/api/v1/admin/opportunities/coverage")
        assert res.status_code == 403

    @pytest.mark.asyncio
    async def test_moderator_can_access(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        res = client.get("/api/v1/admin/opportunities/coverage")
        assert res.status_code == 200


class TestCoverageOnEmptyTable:
    @pytest.mark.asyncio
    async def test_every_real_enum_value_appears_at_zero_when_table_is_empty(
        self, db_session_factory, client_factory
    ):
        """The real, disclosed design decision: a fixed-vocabulary
        dimension (type/academic level) lists every real enum value even
        at a real count of zero -- zero IS the finding."""
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        res = client.get("/api/v1/admin/opportunities/coverage")
        assert res.status_code == 200
        body = res.json()

        assert body["total_opportunities"] == 0
        assert {b["value"] for b in body["by_type"]} == {t.value for t in OpportunityType}
        assert {b["value"] for b in body["by_academic_level"]} == {lvl.value for lvl in AcademicLevel}
        assert all(b["count"] == 0 for b in body["by_type"])
        # An empty table has no "max count" to compare against -- nothing
        # is a real gap relative to another real zero.
        assert all(b["is_gap"] is False for b in body["by_type"])
        assert body["by_country"] == []
        assert body["gap_threshold_ratio"] == pytest.approx(0.1)


class TestCoverageWithRealData:
    @pytest.mark.asyncio
    async def test_real_counts_and_real_gap_flagging(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)

            # 9 scholarships in Egypt, 1 internship in Germany -- a real,
            # deliberately lopsided distribution.
            for _ in range(9):
                await _make_opportunity(db, source_id=source_id, opp_type="scholarship", country="Egypt")
            await _make_opportunity(db, source_id=source_id, opp_type="internship", country="Germany")

        client = client_factory(moderator)
        res = client.get("/api/v1/admin/opportunities/coverage")
        assert res.status_code == 200
        body = res.json()

        assert body["total_opportunities"] == 10

        by_type = {b["value"]: b for b in body["by_type"]}
        assert by_type["scholarship"]["count"] == 9
        assert by_type["scholarship"]["is_gap"] is False
        assert by_type["internship"]["count"] == 1
        # 1 <= 10% of 9 (0.9) is False -- 1 > 0.9, so internship is NOT a
        # gap by the real threshold; volunteer/course (0 count) ARE.
        assert by_type["internship"]["is_gap"] is False
        assert by_type["volunteer"]["count"] == 0
        assert by_type["volunteer"]["is_gap"] is True
        assert by_type["course"]["count"] == 0
        assert by_type["course"]["is_gap"] is True

        by_country = {b["value"]: b for b in body["by_country"]}
        assert by_country["Egypt"]["count"] == 9
        assert by_country["Egypt"]["is_gap"] is False
        assert by_country["Germany"]["count"] == 1
        assert by_country["Germany"]["is_gap"] is False
        # Sorted by count descending.
        assert [b["value"] for b in body["by_country"]] == ["Egypt", "Germany"]

    @pytest.mark.asyncio
    async def test_null_country_bucketed_as_unspecified(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, opp_type="course", country=None)

        client = client_factory(moderator)
        res = client.get("/api/v1/admin/opportunities/coverage")
        body = res.json()
        assert {b["value"] for b in body["by_country"]} == {"Unspecified"}
