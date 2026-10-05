"""Behavioral tests for Review of Aggregated Opportunities Before Publish:

- `Opportunity.review_status` defaulting to `PENDING_REVIEW` for a new
  row (the ORM-level `default=`, exercised directly -- no HTTP needed).
- The real pre-publish gate across all three real browsing-surface query
  sites this step updated: `GET /opportunities/` (`published` query
  param, renamed from `is_verified`), `GET /opportunities/{id}`
  (previously had NO gate at all -- a real, disclosed, now-closed gap),
  and tier gating + the actual approve/reject write path
  (`GET /admin/opportunities/review-queue`,
  `POST /admin/opportunities/{id}/approve`, `.../reject`).
- Approval makes a pending opportunity visible in the real public
  listing; rejection keeps it hidden -- both asserted end-to-end via the
  same public `GET /opportunities/` endpoint a student would actually
  call, not just by re-reading the DB row.
- The Claim System reuse (`ModerationTargetType.OPPORTUNITY` in
  `admin_claims.py`'s `_CLAIMABLE_MODELS`) via the SAME generic
  `/admin/moderation/{item_type}/{item_id}/claim` endpoints every other
  claimable queue already uses -- zero new claim routes.
- The explicit regression risk this step's own Step 4 instruction names:
  an opportunity that was ALREADY `is_verified=True`/live before this
  feature must NOT be silently hidden -- simulated here as a row
  inserted with `review_status='published'` (migration 0044's own real
  backfill value for such a row) and asserted still visible by default.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique as
`tests/test_repeat_offenders.py`/`tests/test_moderation_claims.py`.
"""

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType, OpportunityReviewStatus
from app.models.moderation_log import ModerationLogEntry
from app.models.opportunity import Opportunity
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
    trust_score FLOAT NOT NULL DEFAULT 0.5, access_status VARCHAR(20) NOT NULL DEFAULT 'approved',
    source_type VARCHAR(20) NOT NULL DEFAULT 'website',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# review_status/claimed_by/claimed_at -- this feature's own new columns.
# NO SQL-level DEFAULT here (unlike the 7 pre-existing files' fixtures,
# patched to default 'published' so their unrelated, already-passing
# tests are unaffected) -- every row in THIS file sets review_status
# explicitly and deliberately per test, since the gate itself is exactly
# what's under test.
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
    review_status VARCHAR(20) NOT NULL,
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


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None, full_name="Test User") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        admin_role=admin_role,
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


async def _make_opportunity(
    db: AsyncSession,
    *,
    source_id: uuid.UUID,
    review_status: str,
    created_at: dt.datetime | None = None,
    **overrides,
) -> uuid.UUID:
    """Raw INSERT (`.hex` UUID-encoding -- same fix `tests/test_dashboards.
    py`'s own `_make_opportunity` documents) -- deliberately explicit
    `review_status` on every call, no default, since it's the exact
    thing under test in this file."""
    opportunity_id = overrides.pop("id", uuid.uuid4())
    params = {
        "id": opportunity_id.hex,
        "title": overrides.pop("title", "Some Opportunity"),
        "target_url": "https://example.com/apply",
        "source_id": source_id.hex,
        "type": "scholarship",
        "is_verified": overrides.pop("is_verified", False),
        "review_status": review_status,
    }
    columns = list(params.keys())
    values_sql = ", ".join(f":{c}" for c in columns)
    columns_sql = ", ".join(columns)

    if created_at is not None:
        columns.append("created_at")
        columns_sql += ", created_at"
        values_sql += ", :created_at"
        params["created_at"] = created_at

    await db.execute(
        text(f"INSERT INTO opportunities ({columns_sql}) VALUES ({values_sql})"),
        params,
    )
    await db.commit()
    return opportunity_id


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


# ---------------------------------------------------------------------
# Default-pending-on-create
# ---------------------------------------------------------------------


class TestDefaultPendingOnCreate:
    @pytest.mark.asyncio
    async def test_new_opportunity_row_defaults_to_pending_review(self, db_session_factory):
        """A new `Opportunity` created through the ORM without an
        explicit `review_status` picks up the real column default
        (`OpportunityReviewStatus.PENDING_REVIEW`) -- the literal "new
        aggregated/scraped opportunities default into the pending state"
        requirement, exercised directly against the model rather than
        inferred from the migration."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity = Opportunity(
                id=uuid.uuid4(),
                title="Freshly Scraped Scholarship",
                target_url="https://example.com/new",
                source_id=source_id,
                type="scholarship",
            )
            db.add(opportunity)
            await db.commit()
            await db.refresh(opportunity)

            assert opportunity.review_status == OpportunityReviewStatus.PENDING_REVIEW.value


# ---------------------------------------------------------------------
# Public visibility -- GET /opportunities/ and GET /opportunities/{id}
# ---------------------------------------------------------------------


class TestPublicListingVisibility:
    @pytest.mark.asyncio
    async def test_default_listing_shows_only_published(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            published_id = await _make_opportunity(
                db, source_id=source_id, review_status="published", title="Published One"
            )
            await _make_opportunity(db, source_id=source_id, review_status="pending_review", title="Pending One")
            await _make_opportunity(db, source_id=source_id, review_status="rejected", title="Rejected One")

        client = client_factory(None)
        response = client.get("/api/v1/opportunities/")
        assert response.status_code == 200
        body = response.json()
        ids = {item["id"] for item in body["items"]}
        assert ids == {str(published_id)}
        assert body["total"] == 1

    @pytest.mark.asyncio
    async def test_published_false_shows_pending_and_rejected_not_published(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            await _make_opportunity(db, source_id=source_id, review_status="published", title="Published One")
            pending_id = await _make_opportunity(
                db, source_id=source_id, review_status="pending_review", title="Pending One"
            )
            rejected_id = await _make_opportunity(
                db, source_id=source_id, review_status="rejected", title="Rejected One"
            )

        client = client_factory(None)
        response = client.get("/api/v1/opportunities/?published=false")
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["items"]}
        assert ids == {str(pending_id), str(rejected_id)}

    @pytest.mark.asyncio
    async def test_already_live_opportunity_is_not_hidden_by_this_feature(
        self, db_session_factory, client_factory
    ):
        """The explicit regression risk this step's own Step 4 names:
        an opportunity that was ALREADY `is_verified=True`/visible
        before this feature shipped -- simulated exactly as migration
        0044's own real backfill leaves it (`review_status='published'`)
        -- must still appear in the default public listing afterward."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            already_live_id = await _make_opportunity(
                db,
                source_id=source_id,
                review_status="published",
                is_verified=True,
                title="Pre-Existing Live Opportunity",
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )

        client = client_factory(None)
        response = client.get("/api/v1/opportunities/")
        assert response.status_code == 200
        ids = {item["id"] for item in response.json()["items"]}
        assert str(already_live_id) in ids

    @pytest.mark.asyncio
    async def test_get_by_id_published_is_visible(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="published")

        client = client_factory(None)
        response = client.get(f"/api/v1/opportunities/{opportunity_id}")
        assert response.status_code == 200
        assert response.json()["id"] == str(opportunity_id)

    @pytest.mark.asyncio
    async def test_get_by_id_pending_review_is_404_not_leaked(self, db_session_factory, client_factory):
        """Real, disclosed gap this step closes: before this step,
        `GET /opportunities/{id}` had NO review_status/is_verified check
        at all -- any real id, published or not, was fetchable by
        anyone. A pending-review row must now 404 exactly like a
        genuinely nonexistent id -- not leak its existence via a
        different status code."""
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(None)
        response = client.get(f"/api/v1/opportunities/{opportunity_id}")
        assert response.status_code == 404

    @pytest.mark.asyncio
    async def test_get_by_id_rejected_is_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="rejected")

        client = client_factory(None)
        response = client.get(f"/api/v1/opportunities/{opportunity_id}")
        assert response.status_code == 404


# ---------------------------------------------------------------------
# Review queue + approve/reject -- tier gating, real write path
# ---------------------------------------------------------------------


class TestReviewQueueTierGating:
    @pytest.mark.asyncio
    async def test_student_forbidden_from_review_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db, admin_role=None)

        client = client_factory(student)
        response = client.get("/api/v1/admin/opportunities/review-queue")
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_moderator_can_view_review_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            pending_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")
            await _make_opportunity(db, source_id=source_id, review_status="published")

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/opportunities/review-queue")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["id"] == str(pending_id)

    @pytest.mark.asyncio
    async def test_review_queue_oldest_first(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            older_id = await _make_opportunity(
                db,
                source_id=source_id,
                review_status="pending_review",
                title="Older",
                created_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )
            newer_id = await _make_opportunity(
                db,
                source_id=source_id,
                review_status="pending_review",
                title="Newer",
                created_at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
            )

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/opportunities/review-queue")
        ids = [item["id"] for item in response.json()["items"]]
        assert ids == [str(older_id), str(newer_id)]


class TestApproveRejectTierGating:
    @pytest.mark.asyncio
    async def test_student_forbidden_from_approve(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db, admin_role=None)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(student)
        response = client.post(f"/api/v1/admin/opportunities/{opportunity_id}/approve", json={})
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_student_forbidden_from_reject(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db, admin_role=None)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(student)
        response = client.post(f"/api/v1/admin/opportunities/{opportunity_id}/reject", json={})
        assert response.status_code == 403

    @pytest.mark.asyncio
    async def test_nonexistent_opportunity_404s(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)

        client = client_factory(moderator)
        response = client.post(f"/api/v1/admin/opportunities/{uuid.uuid4()}/approve", json={})
        assert response.status_code == 404


class TestApproveMakesVisible:
    @pytest.mark.asyncio
    async def test_approve_flips_status_and_logs(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/opportunities/{opportunity_id}/approve", json={"notes": "Looks legitimate"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["new_status"] == "published"

        async with db_session_factory() as db:
            log_entries = (
                (await db.execute(select(ModerationLogEntry))).scalars().all()
            )
            assert len(log_entries) == 1
            entry = log_entries[0]
            assert entry.action == ModerationAction.OPPORTUNITY_APPROVE.value
            assert entry.target_type == ModerationTargetType.OPPORTUNITY.value
            assert entry.target_id == opportunity_id
            assert entry.actor_id == moderator.id

    @pytest.mark.asyncio
    async def test_approved_opportunity_becomes_visible_in_real_public_listing(
        self, db_session_factory, client_factory
    ):
        """End-to-end through the REAL public endpoint a student would
        actually call -- not just re-reading the DB row after the
        approve call."""
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        mod_client = client_factory(moderator)
        approve_response = mod_client.post(f"/api/v1/admin/opportunities/{opportunity_id}/approve", json={})
        assert approve_response.status_code == 200

        public_client = client_factory(None)
        listing = public_client.get("/api/v1/opportunities/")
        ids = {item["id"] for item in listing.json()["items"]}
        assert str(opportunity_id) in ids

        by_id = public_client.get(f"/api/v1/opportunities/{opportunity_id}")
        assert by_id.status_code == 200


class TestRejectKeepsHidden:
    @pytest.mark.asyncio
    async def test_reject_flips_status_and_logs(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/opportunities/{opportunity_id}/reject",
            json={"notes": "Looks like a placeholder/test listing"},
        )
        assert response.status_code == 200
        assert response.json()["new_status"] == "rejected"

        async with db_session_factory() as db:
            log_entries = (await db.execute(select(ModerationLogEntry))).scalars().all()
            assert len(log_entries) == 1
            assert log_entries[0].action == ModerationAction.OPPORTUNITY_REJECT.value
            assert log_entries[0].outcome["notes"] == "Looks like a placeholder/test listing"

    @pytest.mark.asyncio
    async def test_rejected_opportunity_stays_hidden_from_real_public_listing(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        mod_client = client_factory(moderator)
        reject_response = mod_client.post(f"/api/v1/admin/opportunities/{opportunity_id}/reject", json={})
        assert reject_response.status_code == 200

        public_client = client_factory(None)
        listing = public_client.get("/api/v1/opportunities/")
        ids = {item["id"] for item in listing.json()["items"]}
        assert str(opportunity_id) not in ids

        by_id = public_client.get(f"/api/v1/opportunities/{opportunity_id}")
        assert by_id.status_code == 404

    @pytest.mark.asyncio
    async def test_rejected_row_is_not_deleted_stays_in_db(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(moderator)
        client.post(f"/api/v1/admin/opportunities/{opportunity_id}/reject", json={})

        async with db_session_factory() as db:
            row = await db.get(Opportunity, opportunity_id)
            assert row is not None
            assert row.review_status == OpportunityReviewStatus.REJECTED.value


# ---------------------------------------------------------------------
# Claim System reuse -- via the SAME generic claim endpoints every other
# claimable queue already uses, zero new claim routes.
# ---------------------------------------------------------------------


class TestClaimSystemReuse:
    @pytest.mark.asyncio
    async def test_claim_then_appears_claimed_in_review_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Claiming Mod")
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(moderator)
        claim_response = client.post(f"/api/v1/admin/moderation/opportunity/{opportunity_id}/claim")
        assert claim_response.status_code == 200
        assert claim_response.json()["claimed_by"] == str(moderator.id)

        queue = client.get("/api/v1/admin/opportunities/review-queue")
        item = queue.json()["items"][0]
        assert item["claimed_by"] == str(moderator.id)
        assert item["claimed_by_name"] == "Claiming Mod"

    @pytest.mark.asyncio
    async def test_release_then_unclaimed_again(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(moderator)
        client.post(f"/api/v1/admin/moderation/opportunity/{opportunity_id}/claim")
        release_response = client.delete(f"/api/v1/admin/moderation/opportunity/{opportunity_id}/claim")
        assert release_response.status_code == 204

        queue = client.get("/api/v1/admin/opportunities/review-queue")
        item = queue.json()["items"][0]
        assert item["claimed_by"] is None

    @pytest.mark.asyncio
    async def test_second_moderator_gets_409_on_active_claim(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            first_mod = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="First")
            second_mod = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Second")
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        first_client = client_factory(first_mod)
        first_client.post(f"/api/v1/admin/moderation/opportunity/{opportunity_id}/claim")

        second_client = client_factory(second_mod)
        response = second_client.post(f"/api/v1/admin/moderation/opportunity/{opportunity_id}/claim")
        assert response.status_code == 409

    @pytest.mark.asyncio
    async def test_approve_does_not_require_a_claim(self, db_session_factory, client_factory):
        """Step 3's own advisory-only posture (matching every other
        claimable queue): approving/rejecting is NOT gated by claim
        state at all."""
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            source_id = await _make_source(db)
            opportunity_id = await _make_opportunity(db, source_id=source_id, review_status="pending_review")

        client = client_factory(moderator)
        response = client.post(f"/api/v1/admin/opportunities/{opportunity_id}/approve", json={})
        assert response.status_code == 200
