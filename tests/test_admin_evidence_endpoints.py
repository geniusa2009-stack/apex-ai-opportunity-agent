"""Tests for D6's completed admin observability surface: the three new
`GET /admin/opportunities/{id}/evidence|claims|verification` read
endpoints and the one new `GET /admin/opportunities/evidence-stats`
counters endpoint (`app/api/v1/endpoints/admin_opportunities.py`).

Real HTTP layer, via `TestClient` + `get_current_user`/`get_db`
dependency overrides -- same technique as `tests/test_admin_moderation.py`'s
`client_factory` (this step's own verification requirement is about the
real status code/JSON shape crossing the wire, not just the underlying
service functions in isolation, which `tests/test_evidence_verification.py`
already covers directly).

In-memory SQLite, `StaticPool`. Tables: `users` (verbatim from
`test_admin_moderation.py`, minus the video/notification tables this
file doesn't need) + `sources`/`opportunities`/`claims`/`evidence`
(verbatim from `tests/test_evidence_verification.py`'s own fixture) +
`user_bookmarks` (empty, `User.bookmarks` `lazy="selectin"` reason, same
as both of those files).
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

_CREATE_SOURCES_SQL = """
CREATE TABLE sources (
    id VARCHAR(36) PRIMARY KEY, name VARCHAR(255) NOT NULL, base_url VARCHAR(2048) NOT NULL,
    active_status BOOLEAN NOT NULL DEFAULT 1, source_type VARCHAR(20) NOT NULL DEFAULT 'website',
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
    claimed_by VARCHAR(36), claimed_at DATETIME,
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


async def _make_opportunity(db: AsyncSession, *, source_id: uuid.UUID, is_verified: bool = False) -> uuid.UUID:
    opp_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO opportunities (id, title, target_url, source_id, type, is_verified) "
            "VALUES (:id, 'Some Opportunity', 'https://example.com/apply', :source_id, 'scholarship', :is_verified)"
        ),
        {"id": opp_id.hex, "source_id": source_id.hex, "is_verified": is_verified},
    )
    await db.commit()
    return opp_id


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


# ---------------------------------------------------------------------
# Auth/tier gating -- same 403 posture as every other admin route.
# ---------------------------------------------------------------------


class TestGating:
    async def test_evidence_endpoint_403_for_student(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)
        client = client_factory(student)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/evidence")
        assert response.status_code == 403

    async def test_claims_endpoint_403_for_student(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)
        client = client_factory(student)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/claims")
        assert response.status_code == 403

    async def test_verification_endpoint_403_for_student(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)
        client = client_factory(student)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/verification")
        assert response.status_code == 403

    async def test_stats_endpoint_403_for_student(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)
        client = client_factory(student)
        response = client.get("/api/v1/admin/opportunities/evidence-stats")
        assert response.status_code == 403


# ---------------------------------------------------------------------
# 404 on unknown opportunity.
# ---------------------------------------------------------------------


class TestNotFound:
    async def test_evidence_404_for_unknown_opportunity(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/evidence")
        assert response.status_code == 404

    async def test_claims_404_for_unknown_opportunity(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/claims")
        assert response.status_code == 404

    async def test_verification_404_for_unknown_opportunity(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{uuid.uuid4()}/verification")
        assert response.status_code == 404


# ---------------------------------------------------------------------
# Empty-list case: a real opportunity, zero D6 data (legacy row).
# ---------------------------------------------------------------------


class TestEmptyCase:
    async def test_evidence_empty_list_not_error(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/evidence")
        assert response.status_code == 200
        assert response.json() == []

    async def test_claims_empty_list_not_error(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/claims")
        assert response.status_code == 200
        assert response.json() == []

    async def test_verification_no_claims_is_unverified_not_error(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)
        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/verification")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "unverified"
        assert body["evaluated_claims"] == []


# ---------------------------------------------------------------------
# Real, populated case.
# ---------------------------------------------------------------------


class TestPopulatedCase:
    async def test_evidence_and_claims_and_verification_reflect_real_seeded_data(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)

            claim = await ev.get_or_create_claim(
                session, opportunity_id=opp_id, claim_type="deadline", claim_value="2026-12-01"
            )
            await session.commit()
            evidence = await ev.record_evidence(
                session, opportunity_id=opp_id, claim_id=claim.id,
                evidence_text="Real evidence text about the deadline.",
                source_url="https://www.state.gov/scholarships", supports_claim=True,
            )
            await session.commit()
            decision = await ev.decide_verification(session, opportunity_id=opp_id, claims=[claim])
            await session.commit()
            assert decision.status.value == "verified"

        client = client_factory(mod)

        ev_response = client.get(f"/api/v1/admin/opportunities/{opp_id}/evidence")
        assert ev_response.status_code == 200
        ev_body = ev_response.json()
        assert len(ev_body) == 1
        assert ev_body[0]["id"] == str(evidence.id)
        assert ev_body[0]["source_url"] == "https://www.state.gov/scholarships"
        assert ev_body[0]["source_authority"] == "government"
        assert "evidence_metadata" not in ev_body[0]

        claims_response = client.get(f"/api/v1/admin/opportunities/{opp_id}/claims")
        assert claims_response.status_code == 200
        claims_body = claims_response.json()
        assert len(claims_body) == 1
        assert claims_body[0]["claim_type"] == "deadline"
        assert claims_body[0]["evidence_count"] == 1
        assert claims_body[0]["status"] == "supported"

        verification_response = client.get(f"/api/v1/admin/opportunities/{opp_id}/verification")
        assert verification_response.status_code == 200
        verification_body = verification_response.json()
        assert verification_body["status"] == "verified"
        assert len(verification_body["evaluated_claims"]) == 1
        assert verification_body["evaluated_claims"][0]["claim_type"] == "deadline"
        assert str(evidence.id) in verification_body["supporting_evidence_ids"]

    async def test_evidence_filtered_by_claim_id(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)
            opp_id = await _make_opportunity(session, source_id=source_id)

            claim_a = await ev.get_or_create_claim(
                session, opportunity_id=opp_id, claim_type="deadline", claim_value="2026-12-01"
            )
            claim_b = await ev.get_or_create_claim(
                session, opportunity_id=opp_id, claim_type="title", claim_value="Some Scholarship"
            )
            await session.commit()
            await ev.record_evidence(
                session, opportunity_id=opp_id, claim_id=claim_a.id, evidence_text="Deadline evidence.",
                source_url="https://example.com/a", supports_claim=True,
            )
            await ev.record_evidence(
                session, opportunity_id=opp_id, claim_id=claim_b.id, evidence_text="Title evidence.",
                source_url="https://example.com/b", supports_claim=True,
            )
            await session.commit()

        client = client_factory(mod)
        response = client.get(f"/api/v1/admin/opportunities/{opp_id}/evidence", params={"claim_id": str(claim_a.id)})
        assert response.status_code == 200
        body = response.json()
        assert len(body) == 1
        assert body[0]["claim_id"] == str(claim_a.id)


# ---------------------------------------------------------------------
# Stats endpoint: real counts that change when more data is seeded.
# ---------------------------------------------------------------------


class TestEvidenceStats:
    async def test_stats_reflect_zero_state(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
        client = client_factory(mod)
        response = client.get("/api/v1/admin/opportunities/evidence-stats")
        assert response.status_code == 200
        body = response.json()
        assert body["evidence_created_total"] == 0
        assert body["evidence_duplicate_total"] is None
        assert body["verification_verified_total"] == 0
        assert isinstance(body["caveats"], list) and body["caveats"]

    async def test_stats_counts_increase_with_seeded_data(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod = await _make_user(session, is_superuser=True)
            source_id = await _make_source(session)

            # One VERIFIED opportunity (real is_verified=True path).
            opp_verified = await _make_opportunity(session, source_id=source_id, is_verified=True)
            claim_v = await ev.get_or_create_claim(
                session, opportunity_id=opp_verified, claim_type="deadline", claim_value="2026-12-01"
            )
            await session.commit()
            await ev.record_evidence(
                session, opportunity_id=opp_verified, claim_id=claim_v.id, evidence_text="Supports the deadline.",
                source_url="https://www.state.gov/apply", supports_claim=True,
            )
            await session.commit()
            await ev.decide_verification(session, opportunity_id=opp_verified, claims=[claim_v])
            await session.commit()

            # One CONTRADICTED/unverified opportunity, never flagged is_verified.
            opp_unverified = await _make_opportunity(session, source_id=source_id, is_verified=False)
            claim_u = await ev.get_or_create_claim(
                session, opportunity_id=opp_unverified, claim_type="deadline", claim_value="2026-01-01"
            )
            await session.commit()
            await ev.record_evidence(
                session, opportunity_id=opp_unverified, claim_id=claim_u.id, evidence_text="Contradicts the deadline.",
                source_url="https://example.com/other", supports_claim=False,
            )
            await session.commit()
            await ev.decide_verification(session, opportunity_id=opp_unverified, claims=[claim_u])
            await session.commit()

        client = client_factory(mod)
        response = client.get("/api/v1/admin/opportunities/evidence-stats")
        assert response.status_code == 200
        body = response.json()
        assert body["evidence_created_total"] == 2
        assert body["claims_evaluated_total"] == 2
        assert body["claims_supported_total"] == 1
        assert body["claims_contradicted_total"] == 1
        assert body["verification_verified_total"] == 1
        assert body["verification_unverified_total"] == 1
