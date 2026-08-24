"""Behavioral tests for the Claim System (Prevent Duplicate Work on the
Same Case) step: `app/services/moderation_claim_service.py`'s real
claim/release/expiry logic, `app/api/v1/endpoints/admin_claims.py`'s two
real HTTP endpoints, the review-queue listing's claim-state display, and
this step's own Step 3 advisory-only enforcement decision (see that
router's own module docstring for the full reasoning).

Real HTTP layer, via `TestClient` + `get_current_user`/`get_db`
dependency overrides -- same technique as `tests/test_admin_roles.py`.
In-memory SQLite, `StaticPool`, same raw-SQL table shape as that file
(`users`/`user_bookmarks`/`student_videos` -- the last now carrying
`claimed_by`/`claimed_at`, migration 0039).
"""

import datetime as dt
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
from app.models.badges import Badge, GamificationProfile, UserBadge
from app.models.enums import AdminRole, StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import moderation_claim_service

# Deliberately NOT a module-level `pytestmark` -- `TestClaimServiceUnit`
# below is entirely synchronous (plain `StudentVideo` objects, no DB);
# only the HTTP-layer test classes genuinely await async fixtures. Same
# established split as `tests/test_storage_cleanup.py`.

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

# Real column list, INCLUDING `claimed_by`/`claimed_at` (migration
# 0039) -- this suite's entire premise rests on them.
_CREATE_STUDENT_VIDEOS_TABLE_SQL = """
CREATE TABLE student_videos (
    id VARCHAR(36) PRIMARY KEY,
    student_id VARCHAR(36) NOT NULL,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    hls_master_url VARCHAR(500),
    status VARCHAR(20) NOT NULL DEFAULT 'pending_ai',
    ai_score INTEGER,
    ai_safety_flag BOOLEAN NOT NULL DEFAULT 0,
    ai_feedback TEXT,
    summary_embedding TEXT,
    duration_seconds INTEGER,
    transcript_text TEXT,
    transcript_segments TEXT,
    thumbnail_url VARCHAR(500),
    admin_override BOOLEAN NOT NULL DEFAULT 0,
    admin_notes TEXT,
    claimed_by VARCHAR(36),
    claimed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# `moderation_log_entries` -- needed because `override_video_decision`
# (exercised by the Step 3 advisory-enforcement proof below) writes a
# real log entry in the same transaction. Same raw-SQL/JSONB-as-TEXT
# pattern as every other file's fixture since the Automatic Moderation
# Activity Log step.
_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY,
    actor_id VARCHAR(36),
    actor_role VARCHAR(20) NOT NULL,
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(20) NOT NULL,
    target_id VARCHAR(36) NOT NULL,
    outcome TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Needed because `TestClaimIsAdvisoryNotEnforced`'s own proof calls the
# real `override_video_decision` endpoint with `target_status=approved`
# -- that branch calls `notification_service.create_notification`. Same
# raw-SQL/JSONB-as-TEXT pattern as every other file's fixture.
_CREATE_NOTIFICATIONS_TABLE_SQL = """
CREATE TABLE notifications (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    is_read BOOLEAN NOT NULL DEFAULT 0,
    payload TEXT,
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
        await conn.execute(text(_CREATE_STUDENT_VIDEOS_TABLE_SQL))
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[GamificationProfile.__table__, Badge.__table__, UserBadge.__table__],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession,
    *,
    is_superuser: bool = False,
    admin_role: AdminRole | None = None,
    full_name: str = "Test User",
) -> User:
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name,
        is_superuser=is_superuser, admin_role=admin_role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str, **overrides) -> StudentVideo:
    video_id = overrides.pop("id", uuid.uuid4())
    defaults = dict(id=video_id, student_id=student_id, title="Test Video", status=status, ai_safety_flag=False)
    defaults.update(overrides)
    video = StudentVideo(**defaults)
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


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


def _claim_url(video_id: uuid.UUID) -> str:
    return f"/api/v1/admin/moderation/student_video/{video_id}/claim"


# ---------------------------------------------------------------------
# Service-layer: get_effective_claim / apply_claim / apply_release
# ---------------------------------------------------------------------


class TestClaimServiceUnit:
    def test_unclaimed_video_has_no_effective_claim(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        claimed_by, claimed_at = moderation_claim_service.get_effective_claim(video)
        assert claimed_by is None
        assert claimed_at is None

    def test_apply_claim_on_unclaimed_video_sets_caller_as_claimant(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        caller_id = uuid.uuid4()
        moderation_claim_service.apply_claim(video, caller_id=caller_id)
        assert video.claimed_by == caller_id
        assert video.claimed_at is not None

    def test_apply_claim_conflict_when_claimed_by_someone_else(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        first_claimant = uuid.uuid4()
        moderation_claim_service.apply_claim(video, caller_id=first_claimant)

        with pytest.raises(moderation_claim_service.ClaimConflictError) as exc_info:
            moderation_claim_service.apply_claim(video, caller_id=uuid.uuid4())

        assert exc_info.value.claimed_by == first_claimant
        # Conflict must NOT have mutated the video.
        assert video.claimed_by == first_claimant

    def test_apply_claim_is_idempotent_and_refreshes_timestamp_for_same_caller(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        caller_id = uuid.uuid4()
        moderation_claim_service.apply_claim(video, caller_id=caller_id)
        first_claimed_at = video.claimed_at

        moderation_claim_service.apply_claim(video, caller_id=caller_id)
        assert video.claimed_by == caller_id
        assert video.claimed_at >= first_claimed_at

    def test_expired_claim_is_effectively_unclaimed(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        video.claimed_by = uuid.uuid4()
        video.claimed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            minutes=moderation_claim_service.CLAIM_TIMEOUT_MINUTES + 1
        )

        claimed_by, claimed_at = moderation_claim_service.get_effective_claim(video)
        assert claimed_by is None
        assert claimed_at is None

    def test_claim_just_under_the_timeout_is_still_active(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        original_claimant = uuid.uuid4()
        video.claimed_by = original_claimant
        video.claimed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            minutes=moderation_claim_service.CLAIM_TIMEOUT_MINUTES - 1
        )

        claimed_by, claimed_at = moderation_claim_service.get_effective_claim(video)
        assert claimed_by == original_claimant
        assert claimed_at is not None

    def test_a_new_claim_over_an_expired_one_succeeds_and_overwrites(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        stale_claimant = uuid.uuid4()
        video.claimed_by = stale_claimant
        video.claimed_at = dt.datetime.now(dt.timezone.utc) - dt.timedelta(
            minutes=moderation_claim_service.CLAIM_TIMEOUT_MINUTES + 5
        )

        new_claimant = uuid.uuid4()
        moderation_claim_service.apply_claim(video, caller_id=new_claimant)
        assert video.claimed_by == new_claimant

    def test_apply_release_clears_claim_unconditionally(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        moderation_claim_service.apply_claim(video, caller_id=uuid.uuid4())
        moderation_claim_service.apply_release(video)
        assert video.claimed_by is None
        assert video.claimed_at is None

    def test_apply_release_on_already_unclaimed_video_is_a_harmless_noop(self):
        video = StudentVideo(id=uuid.uuid4(), student_id=uuid.uuid4(), title="t", status="pending_ai")
        moderation_claim_service.apply_release(video)
        assert video.claimed_by is None
        assert video.claimed_at is None


# ---------------------------------------------------------------------
# Real HTTP: POST .../claim
# ---------------------------------------------------------------------


class TestClaimEndpoint:
    pytestmark = pytest.mark.asyncio

    async def test_moderator_can_claim_an_unclaimed_video(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Mod One")
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(moderator)
        response = client.post(_claim_url(video.id))
        assert response.status_code == 200
        body = response.json()
        assert body["item_type"] == "student_video"
        assert body["item_id"] == str(video.id)
        assert body["claimed_by"] == str(moderator.id)
        assert body["claimed_by_name"] == "Mod One"
        assert body["claimed_at"] is not None

    async def test_second_moderator_gets_409_on_an_actively_claimed_video(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            mod_a = await _make_user(session, admin_role=AdminRole.MODERATOR)
            mod_b = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client_factory(mod_a).post(_claim_url(video.id))
        response_b = client_factory(mod_b).post(_claim_url(video.id))
        assert response_b.status_code == 409

    async def test_reclaiming_your_own_claim_is_idempotent_200(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(moderator)
        first = client.post(_claim_url(video.id))
        second = client.post(_claim_url(video.id))
        assert first.status_code == 200
        assert second.status_code == 200
        assert second.json()["claimed_by"] == str(moderator.id)

    async def test_claiming_an_expired_claim_succeeds_for_a_different_moderator(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            stale_claimant = await _make_user(session, admin_role=AdminRole.MODERATOR)
            new_claimant = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(
                session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value,
                claimed_by=stale_claimant.id,
                claimed_at=dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(minutes=moderation_claim_service.CLAIM_TIMEOUT_MINUTES + 10),
            )

        client = client_factory(new_claimant)
        response = client.post(_claim_url(video.id))
        assert response.status_code == 200
        assert response.json()["claimed_by"] == str(new_claimant.id)

    async def test_claiming_a_nonexistent_video_404s(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.post(_claim_url(uuid.uuid4()))
        assert response.status_code == 404

    async def test_claiming_an_unsupported_item_type_404s(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        # "user" is a real `ModerationTargetType` member (chat-unban's
        # target kind) but is deliberately NOT in `_CLAIMABLE_MODELS` --
        # there's no queue of "users" a volunteer opens and works on.
        response = client.post(f"/api/v1/admin/moderation/user/{uuid.uuid4()}/claim")
        assert response.status_code == 404

    async def test_plain_student_gets_403_on_claim(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(student)
        response = client.post(_claim_url(video.id))
        assert response.status_code == 403


# ---------------------------------------------------------------------
# Real HTTP: DELETE .../claim (release)
# ---------------------------------------------------------------------


class TestReleaseEndpoint:
    pytestmark = pytest.mark.asyncio

    async def test_claimant_can_release_their_own_claim(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(moderator)
        client.post(_claim_url(video.id))
        response = client.delete(_claim_url(video.id))
        assert response.status_code == 204

        async with db_session_factory() as session:
            refreshed = await session.get(StudentVideo, video.id)
            assert refreshed.claimed_by is None
            assert refreshed.claimed_at is None

    async def test_non_claimant_non_owner_gets_403_on_release(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            claimant = await _make_user(session, admin_role=AdminRole.MODERATOR)
            other_moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client_factory(claimant).post(_claim_url(video.id))
        response = client_factory(other_moderator).delete(_claim_url(video.id))
        assert response.status_code == 403

    async def test_owner_can_force_release_someone_elses_claim(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            claimant = await _make_user(session, admin_role=AdminRole.MODERATOR)
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client_factory(claimant).post(_claim_url(video.id))
        response = client_factory(owner).delete(_claim_url(video.id))
        assert response.status_code == 204

        async with db_session_factory() as session:
            refreshed = await session.get(StudentVideo, video.id)
            assert refreshed.claimed_by is None

    async def test_releasing_an_already_unclaimed_video_is_idempotent_204(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(moderator)
        response = client.delete(_claim_url(video.id))
        assert response.status_code == 204

    async def test_releasing_a_nonexistent_video_404s(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.delete(_claim_url(uuid.uuid4()))
        assert response.status_code == 404


# ---------------------------------------------------------------------
# Real HTTP: review-queue listing shows claim state
# ---------------------------------------------------------------------


class TestReviewQueueClaimState:
    pytestmark = pytest.mark.asyncio

    async def test_review_queue_shows_active_claim_with_claimant_name(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Claim Holder")
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(moderator)
        client.post(_claim_url(video.id))

        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 200
        items = response.json()["items"]
        item = next(i for i in items if i["id"] == str(video.id))
        assert item["claimed_by"] == str(moderator.id)
        assert item["claimed_by_name"] == "Claim Holder"
        assert item["claimed_at"] is not None

    async def test_review_queue_shows_unclaimed_item_as_null(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.FAILED.value)

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/videos/review-queue")
        item = next(i for i in response.json()["items"] if i["id"] == str(video.id))
        assert item["claimed_by"] is None
        assert item["claimed_by_name"] is None
        assert item["claimed_at"] is None

    async def test_review_queue_shows_expired_claim_as_unclaimed(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            stale_claimant = await _make_user(session, admin_role=AdminRole.MODERATOR)
            viewer = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(
                session, student_id=student.id, status=StudentVideoStatus.REJECTED.value,
                claimed_by=stale_claimant.id,
                claimed_at=dt.datetime.now(dt.timezone.utc)
                - dt.timedelta(minutes=moderation_claim_service.CLAIM_TIMEOUT_MINUTES + 1),
            )

        client = client_factory(viewer)
        response = client.get("/api/v1/admin/videos/review-queue")
        item = next(i for i in response.json()["items"] if i["id"] == str(video.id))
        assert item["claimed_by"] is None
        assert item["claimed_by_name"] is None


# ---------------------------------------------------------------------
# Step 3: advisory-only enforcement decision (NOT a hard lock).
# ---------------------------------------------------------------------


class TestClaimIsAdvisoryNotEnforced:
    pytestmark = pytest.mark.asyncio

    async def test_a_different_moderator_can_still_override_a_video_claimed_by_someone_else(
        self, db_session_factory, client_factory
    ):
        """This IS this step's own Step 3 decision, proven behaviorally:
        claiming is a visibility aid, not an enforced lock. `mod_b`
        never claims (or even looks at) the video, yet their real write
        action still succeeds even though `mod_a` actively holds the
        claim."""
        async with db_session_factory() as session:
            mod_a = await _make_user(session, admin_role=AdminRole.MODERATOR)
            mod_b = await _make_user(session, admin_role=AdminRole.MODERATOR)
            student = await _make_user(session)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        client_factory(mod_a).post(_claim_url(video.id))

        response = client_factory(mod_b).post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "approved", "notes": "acting despite another volunteer's claim"},
        )
        assert response.status_code == 200
        assert response.json()["new_status"] == "approved"
