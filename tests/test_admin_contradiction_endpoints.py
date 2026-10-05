"""D7 (Contradiction Hunter & Adversarial Verification) admin surface
tests: the new `GET /admin/opportunities/{id}/contradictions` endpoint
and the extension of `GET /admin/opportunities/{id}/verification` to
also surface real `Contradiction` rows.

Real HTTP layer, `TestClient` + `get_current_user`/`get_db` overrides --
same technique as `tests/test_admin_evidence_endpoints.py`. In-memory
SQLite, `StaticPool`.
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
from app.models.user import User
from app.services import evidence_verification as ev

pytestmark = pytest.mark.asyncio

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

_CREATE_USER_BOOKMARKS_TABLE_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_SOURCES_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1, trust_score FLOAT NOT NULL DEFAULT 0.5, access_status VARCHAR(20) NOT NULL DEFAULT 'approved',
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_OPPORTUNITIES_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY, title VARCHAR(500) NOT NULL, description TEXT,
    target_url VARCHAR(2048) NOT NULL, source_id VARCHAR(36) NOT NULL, type VARCHAR(20) NOT NULL,
    deadline DATETIME, eligibility_criteria TEXT, funding_details TEXT, location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global', is_verified BOOLEAN NOT NULL DEFAULT 0, ai_confidence_score FLOAT,
    raw_extracted_data TEXT, embedding TEXT, is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50), target_academic_levels TEXT, min_age INTEGER, max_age INTEGER,
    target_gender VARCHAR(10), review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36), claimed_at DATETIME, lifecycle_state VARCHAR(30) NOT NULL DEFAULT 'discovered', lifecycle_state_reason TEXT, lifecycle_state_evaluated_at DATETIME, monitoring_last_checked_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_CLAIMS_SQL = """
CREATE TABLE claims (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_type VARCHAR(50) NOT NULL,
    claim_value TEXT NOT NULL, normalized_value TEXT, status VARCHAR(20) NOT NULL DEFAULT 'unknown',
    importance VARCHAR(20) NOT NULL DEFAULT 'normal',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_EVIDENCE_SQL = """
CREATE TABLE evidence (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_id VARCHAR(36) NOT NULL,
    source_id VARCHAR(36), source_url VARCHAR(2048), source_type VARCHAR(50),
    source_authority VARCHAR(20) NOT NULL DEFAULT 'unknown', evidence_text TEXT NOT NULL,
    evidence_hash VARCHAR(64) NOT NULL, retrieved_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    published_at DATETIME, valid_from DATETIME, valid_until DATETIME, supports_claim BOOLEAN,
    extraction_method VARCHAR(50) NOT NULL DEFAULT 'manual', retrieval_method VARCHAR(50) NOT NULL DEFAULT 'automated',
    evidence_metadata TEXT, created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_CONTRADICTIONS_SQL = """
CREATE TABLE contradictions (
    id VARCHAR(36) PRIMARY KEY, opportunity_id VARCHAR(36) NOT NULL, claim_id VARCHAR(36) NOT NULL,
    supporting_evidence_id VARCHAR(36), contradicting_evidence_id VARCHAR(36) NOT NULL,
    contradiction_type VARCHAR(30) NOT NULL, severity VARCHAR(20) NOT NULL,
    source_authority VARCHAR(20) NOT NULL DEFAULT 'unknown',
    detected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP, explanation TEXT NOT NULL,
    contradiction_metadata TEXT
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine(
        "sqlite+aiosqlite:///:memory:", poolclass=StaticPool, connect_args={"check_same_thread": False},
    )
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        await conn.execute(text(_CREATE_SOURCES_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_SQL))
        await conn.execute(text(_CREATE_CLAIMS_SQL))
        await conn.execute(text(_CREATE_EVIDENCE_SQL))
        await conn.execute(text(_CREATE_CONTRADICTIONS_SQL))
    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, is_superuser: bool) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test User", is_superuser=is_superuser)
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


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID) -> uuid.UUID:
    opp_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO opportunities (id, title, target_url, source_id, type) "
            "VALUES (:id, 'Some Opportunity', 'https://example.com/apply', :source_id, 'scholarship')"
        ),
        {"id": opp_id.hex, "source_id": source_id.hex},
    )
    await db.commit()
    return opp_id


async def _make_contradiction(db: AsyncSession, *, opportunity_id: uuid.UUID) -> uuid.UUID:
    """Seeds one real, minimal, self-consistent Contradiction row (plus
    its required Claim/Evidence rows) for one opportunity, via the real
    D6 functions -- never hand-crafted evidence with no claim behind it."""
    claim = await ev.get_or_create_claim(db, opportunity_id=opportunity_id, claim_type="deadline", claim_value="2026-12-01")
    await db.commit()
    supporting = await ev.record_evidence(
        db, opportunity_id=opportunity_id, claim_id=claim.id, evidence_text="Confirms deadline.",
        source_url="https://example.com/apply", supports_claim=True,
    )
    contradicting = await ev.record_evidence(
        db, opportunity_id=opportunity_id, claim_id=claim.id, evidence_text="Official: deadline moved earlier.",
        source_url="https://www.state.gov/notice", supports_claim=False,
    )
    await db.commit()

    contradiction_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO contradictions (id, opportunity_id, claim_id, supporting_evidence_id, "
            "contradicting_evidence_id, contradiction_type, severity, source_authority, explanation) "
            "VALUES (:id, :opp_id, :claim_id, :supporting_id, :contradicting_id, 'deadline_conflict', "
            "'high', 'government', 'Test contradiction explanation.')"
        ),
        {
            "id": contradiction_id.hex, "opp_id": opportunity_id.hex, "claim_id": claim.id.hex,
            "supporting_id": supporting.id.hex, "contradicting_id": contradicting.id.hex,
        },
    )
    await db.commit()
    return contradiction_id


@pytest.fixture
def client_factory(db_session_factory):
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


class TestGatingAndNotFound:
    async def test_contradictions_endpoint_403_for_student(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)
        client = client_factory(student)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/contradictions")
        assert response.status_code == 403

    async def test_contradictions_404_for_unknown_opportunity(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/contradictions")
        assert response.status_code == 404

    async def test_contradictions_empty_list_not_error(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/contradictions")
        assert response.status_code == 200
        assert response.json() == []


class TestPopulatedAndShape:
    async def test_contradictions_reflect_real_seeded_data(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)
            contradiction_id = await _make_contradiction(session, opportunity_id=opp_id)

        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/contradictions")
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["id"] == str(contradiction_id)
        assert body[0]["contradiction_type"] == "deadline_conflict"
        assert body[0]["severity"] == "high"
        assert body[0]["source_authority"] == "government"
        assert "contradiction_metadata" not in body[0]

    async def test_verification_endpoint_includes_contradictions(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)
            contradiction_id = await _make_contradiction(session, opportunity_id=opp_id)

        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/verification")
        assert response.status_code == 200
        body = response.json()
        assert "contradictions" in body
        assert len(body["contradictions"]) == 1
        assert body["contradictions"][0]["id"] == str(contradiction_id)

    async def test_verification_with_no_contradictions_returns_empty_list(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/verification")
        assert response.status_code == 200
        assert response.json()["contradictions"] == []


class TestIDOR:
    """A contradiction seeded under opportunity A must never be
    reachable through opportunity B's `/contradictions` or
    `/verification` path -- same scoping-by-path-opportunity_id pattern
    `get_opportunity_evidence`/`get_opportunity_claims` already use."""

    async def test_contradiction_not_reachable_through_other_opportunitys_path(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_a = await _make_opportunity(session, source_id=source_id)
            opp_b = await _make_opportunity(session, source_id=source_id)
            await _make_contradiction(session, opportunity_id=opp_a)

        client = client_factory(mod)

        response_b_contradictions = client.get(f"/api/v1/admin/opportunities/{opp_b}/contradictions")
        assert response_b_contradictions.status_code == 200
        assert response_b_contradictions.json() == []

        response_b_verification = client.get(f"/api/v1/admin/opportunities/{opp_b}/verification")
        assert response_b_verification.status_code == 200
        assert response_b_verification.json()["contradictions"] == []

        # Sanity: it IS visible through its own real opportunity's path.
        response_a = client.get(f"/api/v1/admin/opportunities/{opp_a}/contradictions")
        assert response_a.status_code == 200
        assert len(response_a.json()) == 1
