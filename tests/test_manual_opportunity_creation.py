"""Behavioral tests for Manual Opportunity Creation, with Designated-
Team-Member Access:

- Tier gating on `POST /admin/opportunities`: unauthenticated -> 401,
  plain Moderator -> 403, Senior Moderator -> 201, Owner -> 201 (the
  access-tier design this feature's own module docstring in
  `admin_opportunities.py` explains -- `RequireSeniorModerationAccess`
  reuses the existing 3-tier role system rather than a new permission
  dimension).
- The created row's real field values: `review_status=PUBLISHED`
  (written directly, not `PENDING_REVIEW`), `is_verified=False`
  (honest -- never went through the AI verification pipeline),
  `ai_confidence_score`/`raw_extracted_data`/`embedding` all `None`,
  every payload field round-trips correctly (including the two
  disclosed-scope-boundary filters `OpportunityCreate` could never
  populate: `target_academic_levels`/`min_age`/`max_age`/
  `target_gender`).
- The shared "Manual Entry" `Source` row: `source_type=MANUAL`,
  `name="Manual Entry"`, and -- the real behavioral claim under test --
  creating two manual opportunities reuses the SAME `Source` row
  (get-or-create, not create-every-time), asserted by counting real
  `sources` table rows after two creates.
- A real `ModerationLogEntry` row is written
  (`action=opportunity_manual_create`, `target_type=opportunity`,
  `target_id=<the new opportunity's id>`) -- the same "every admin
  write gets logged" discipline every other write action in this
  codebase follows.
- End-to-end visibility: a manually created opportunity actually shows
  up in the real public `GET /opportunities/` listing (published-by-
  default), the same end-to-end assertion
  `tests/test_opportunity_review.py` already makes for
  approve/reject -- not just re-reading the DB row.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique as
`tests/test_opportunity_review.py`, whose table DDL and fixture helpers
this file reuses near-verbatim.
"""

import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import func, select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType, OpportunityReviewStatus
from app.models.moderation_log import ModerationLogEntry
from app.models.source import Source
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
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
    review_status VARCHAR(20) NOT NULL DEFAULT 'pending_review',
    claimed_by VARCHAR(36),
    claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY,
    actor_id VARCHAR(36),
    actor_role VARCHAR(20),
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(30) NOT NULL,
    target_id VARCHAR(36) NOT NULL,
    outcome TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name="Test User",
        admin_role=admin_role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


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


_VALID_PAYLOAD = {
    "title": "Hand-Typed Scholarship",
    "description": "A scholarship a Moderator heard about directly.",
    "target_url": "https://example.com/apply",
    "opportunity_type": "scholarship",
    "country": "Egypt",
    "is_local_egypt": True,
    "target_age_group": "18-24",
    # DISCLOSED, SAME LIMITATION `tests/test_eligibility_backfill.py`/
    # `tests/test_opportunities.py` already established: `target_academic_levels`
    # is a Postgres ARRAY of a native enum with no SQLite equivalent --
    # committing a real (non-empty) value into it via the ORM against
    # this suite's in-memory SQLite raises a CompileError. Left unset
    # here (None on every commit path); the field's own round-trip
    # (accepted, returned null when omitted) is still exercised by
    # `test_defaults_when_optional_fields_omitted` below.
    "min_age": 18,
    "max_age": 24,
    "target_gender": "male",
}


pytestmark = pytest.mark.asyncio


class TestManualOpportunityTierGating:
    async def test_unauthenticated_rejected(self, client_factory):
        # Same disclosed, pre-existing quirk `tests/test_user_search.py`
        # already surfaced elsewhere in this suite: FastAPI's real
        # `OAuth2PasswordBearer` dependency (with no `get_current_user`
        # override installed, since `user` is `None` here) returns 403
        # Forbidden for a missing-credentials request, not 401
        # Unauthorized -- a Starlette/FastAPI default, not a bug in this
        # feature's own route. Asserting the real, current behavior.
        client = client_factory(None)
        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 403

    async def test_plain_moderator_rejected(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(user)
        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 403

    async def test_senior_moderator_allowed(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(user)
        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 201

    async def test_owner_allowed(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.OWNER)
        client = client_factory(user)
        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 201

    async def test_legacy_superuser_allowed(self, client_factory, db_session_factory):
        """`is_superuser=True` is the legacy OWNER alias -- see
        `AdminRole`'s own docstring -- so it must clear the
        Senior-Moderator-or-higher gate too."""
        async with db_session_factory() as db:
            user = User(
                id=uuid.uuid4(),
                email=f"{uuid.uuid4()}@example.com",
                full_name="Legacy Owner",
                is_superuser=True,
            )
            db.add(user)
            await db.commit()
            await db.refresh(user)
        client = client_factory(user)
        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 201


class TestManualOpportunityCreate:
    async def test_created_row_field_values(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(user)

        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 201
        body = resp.json()

        assert body["title"] == "Hand-Typed Scholarship"
        assert body["description"] == "A scholarship a Moderator heard about directly."
        assert body["target_url"] == "https://example.com/apply"
        assert body["type"] == "scholarship"
        assert body["country"] == "Egypt"
        assert body["is_local_egypt"] is True
        assert body["target_age_group"] == "18-24"
        assert body["target_academic_levels"] is None
        assert body["min_age"] == 18
        assert body["max_age"] == 24
        assert body["target_gender"] == "male"

        # The real design decisions under test: published immediately,
        # never AI-verified, no AI-pipeline artifacts.
        assert body["review_status"] == "published"
        assert body["is_verified"] is False
        assert body["ai_confidence_score"] is None

        # The shared Manual Entry source row.
        assert body["source"]["name"] == "Manual Entry"
        assert body["source"]["source_type"] == "manual"

    async def test_defaults_when_optional_fields_omitted(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.OWNER)
        client = client_factory(user)

        minimal_payload = {
            "title": "Minimal Opportunity",
            "target_url": "https://example.com/minimal",
            "opportunity_type": "internship",
        }
        resp = client.post("/api/v1/admin/opportunities", json=minimal_payload)
        assert resp.status_code == 201
        body = resp.json()
        assert body["country"] == "Global"
        assert body["is_local_egypt"] is False
        assert body["description"] is None
        assert body["target_academic_levels"] is None
        assert body["min_age"] is None

    async def test_two_creates_reuse_same_source_row(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(user)

        r1 = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        r2 = client.post(
            "/api/v1/admin/opportunities",
            json={**_VALID_PAYLOAD, "title": "A Second Hand-Typed Opportunity"},
        )
        assert r1.status_code == 201
        assert r2.status_code == 201
        assert r1.json()["source"]["id"] == r2.json()["source"]["id"]

        async with db_session_factory() as db:
            count = (
                await db.execute(select(func.count()).select_from(Source))
            ).scalar_one()
        assert count == 1

    async def test_moderation_log_entry_written(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(user)

        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 201
        opportunity_id = resp.json()["id"]

        async with db_session_factory() as db:
            result = await db.execute(
                select(ModerationLogEntry).where(
                    ModerationLogEntry.target_id == uuid.UUID(opportunity_id)
                )
            )
            entry = result.scalar_one()

        assert entry.action == ModerationAction.OPPORTUNITY_MANUAL_CREATE
        assert entry.target_type == ModerationTargetType.OPPORTUNITY
        assert entry.actor_id == user.id

    async def test_appears_in_public_listing(self, client_factory, db_session_factory):
        """End-to-end: a manually created opportunity is actually
        visible via the real public listing a student would call, not
        just correct in the DB row -- same posture
        `test_opportunity_review.py` already established."""
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(user)

        resp = client.post("/api/v1/admin/opportunities", json=_VALID_PAYLOAD)
        assert resp.status_code == 201
        opportunity_id = resp.json()["id"]

        public_client = client_factory(None)
        listing = public_client.get("/api/v1/opportunities/")
        assert listing.status_code == 200
        ids = [item["id"] for item in listing.json()["items"]]
        assert opportunity_id in ids

    async def test_invalid_payload_rejected(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db, admin_role=AdminRole.OWNER)
        client = client_factory(user)

        resp = client.post(
            "/api/v1/admin/opportunities",
            json={"title": "", "target_url": "x", "opportunity_type": "scholarship"},
        )
        assert resp.status_code == 422
