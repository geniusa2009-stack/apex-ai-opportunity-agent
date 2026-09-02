"""Behavioral tests for the Real Tiered Role System step:
`app/api/deps.py`'s three new tier dependencies
(`RequireModerationAccess`/`RequireSeniorModerationAccess`/`RequireOwner`),
their wiring onto the six already-confirmed-real admin/moderation
routes, and the two new Owner-only role-management routes
(`app/api/v1/endpoints/admin_roles.py`).

Real HTTP layer, via `TestClient` + `get_current_user`/`get_db`
dependency overrides -- same technique as `tests/test_admin_moderation.py`,
generalized to build FOUR distinct users (Owner, Senior Moderator,
Moderator, plain student) instead of two.

In-memory SQLite, `StaticPool`. Same four-table shape as
`test_dashboards.py` (`users`/`user_bookmarks`/`student_videos`/
`notifications` -- the last needed because `override_video_decision`'s
own APPROVED-transition branch calls `notification_service.
create_notification`), plus `admin_role` on `users` -- this suite's
entire premise rests on varying it. Also reuses `test_dashboards.py`'s
own `_FakeRedis`/`fake_redis` fixture technique (NO real Redis reachable
in this sandbox -- `GET /dashboards/admin` calls
`chat_security.list_banned_users`, which needs a real or faked Redis
client either way).
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
from app.services import chat_security, notification_connection_manager, notification_service
from app.main import app
from app.models.badges import Badge, GamificationProfile, UserBadge
from app.models.enums import AdminRole, StudentVideoStatus
from app.models.onboarding import OnboardingChecklistProgress
from app.models.student_videos import StudentVideo
from app.models.user import User

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

# Needed only because `GET /dashboards/admin`'s `platform_stats` section
# runs `SELECT count(*) FROM opportunities` -- same table shape as
# `test_dashboards.py`'s own fixture, verbatim.
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

# NEW as of the Automatic Moderation Activity Log step: the three
# moderator-tier write actions this suite already exercises
# (`override_video_decision`, `delete_video_permanently`,
# `reject_video_and_purge_media`) now each write a real
# `ModerationLogEntry` row in the SAME transaction as their own
# mutation -- this fixture's DB needs the table to exist or those
# writes 500 with "no such table". Raw SQL (not `Base.metadata.
# create_all`) for the same reason `notifications.payload` needed it:
# `outcome` is `JSONB`, which SQLite's DDL compiler can't render.
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
        await conn.execute(text(_CREATE_SOURCES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                GamificationProfile.__table__,
                Badge.__table__,
                UserBadge.__table__,
                # New-Volunteer Onboarding Checklist addition -- a
                # first-time grant via `assign_admin_role` now also
                # deletes any stale `OnboardingChecklistProgress` rows
                # for the target user (real reset-on-fresh-grant
                # behavior); the table must exist for that DELETE not
                # to 500 with "no such table" even though this suite's
                # own tests never populate it.
                OnboardingChecklistProgress.__table__,
            ],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


class _FakeRedis:
    """Verbatim copy of `test_dashboards.py`'s own `_FakeRedis` -- see
    that file's module docstring for why a fake is needed at all (no
    real Redis reachable in this sandbox test run)."""

    def __init__(self) -> None:
        self._store: dict[str, tuple[str, int | None]] = {}

    async def set(self, key: str, value: str, ex: int | None = None) -> None:
        self._store[key] = (value, ex)

    async def exists(self, key: str) -> int:
        return 1 if key in self._store else 0

    async def ttl(self, key: str) -> int:
        if key not in self._store:
            return -2
        _, ex = self._store[key]
        return ex if ex is not None else -1

    async def delete(self, key: str) -> int:
        return 1 if self._store.pop(key, None) is not None else 0

    async def scan_iter(self, match: str):
        prefix = match.rstrip("*")
        for key in list(self._store.keys()):
            if key.startswith(prefix):
                yield key


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(chat_security, "_get_redis_client", lambda: redis)
    return redis


@pytest.fixture(autouse=True)
def _fake_notification_broadcast(monkeypatch):
    """DISCLOSED PRE-EXISTING FIXTURE GAP, found and fixed during Full
    Ban-Reason History's own full regression run: this file's
    `test_senior_moderator_can_reject_and_purge` hung indefinitely (same
    root cause, same fix, as the identical gap already found and fixed
    in `tests/test_moderation_log.py` -- see that file's own
    `_fake_notification_broadcast` docstring for the full explanation).
    `reject_video_and_purge_media` calls `notification_service.create_notification`,
    which was never mocked here, so every real DNS-resolution timeout
    against an unreachable Redis/Celery broker ate the run. Copied
    verbatim from `tests/test_chat_reports.py`'s own fixture, same as
    the other file.
    """

    class _NoopPublishClient:
        async def publish(self, channel: str, message: str) -> None:
            return None

    class _NoopEmailTask:
        def delay(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(notification_connection_manager, "_get_redis_client", lambda: _NoopPublishClient())
    monkeypatch.setattr(notification_service, "send_transactional_email_task", _NoopEmailTask())


async def _make_user(
    db: AsyncSession,
    *,
    is_superuser: bool = False,
    admin_role: AdminRole | None = None,
    full_name: str = "Test User",
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        is_superuser=is_superuser,
        admin_role=admin_role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str, **overrides) -> StudentVideo:
    video_id = overrides.pop("id", uuid.uuid4())
    defaults = dict(
        id=video_id, student_id=student_id, title="Test Video", status=status, ai_safety_flag=False
    )
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


# ---------------------------------------------------------------------
# Enum round-trip -- proves the `values_callable` bug from the prior
# step (SQLAlchemy sending the Python enum NAME instead of `.value`)
# does not recur here, since `AdminRole` was built with the fix from the
# start.
# ---------------------------------------------------------------------


class TestAdminRoleEnumRoundTrip:
    async def test_admin_role_persists_and_reloads_as_lowercase_value(self, db_session_factory):
        # This is exactly the shape of bug the prior step fixed on
        # `role`/`academic_level`/`gender`: without `values_callable`,
        # SQLAlchemy would bind the Python enum MEMBER NAME
        # ("SENIOR_MODERATOR") on write, then raise `LookupError` trying
        # to resolve that same string back to an `AdminRole` member on
        # read (since `.value` is "senior_moderator", not
        # "SENIOR_MODERATOR"). A clean write-then-fresh-session-reload
        # with no exception, returning the correct member, IS the proof
        # -- same diagnostic shape used to find and confirm the original
        # bug.
        async with db_session_factory() as session:
            user = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            user_id = user.id

            # Raw SQL on the SAME session/connection (still within the
            # same transaction as the write above) -- reads back exactly
            # what's physically stored in the column, bypassing the
            # ORM's own enum bind/result processing entirely. Filters by
            # `admin_role` alone (a single row exists in this fresh
            # in-memory DB at this point) rather than by `id` -- the raw
            # `id` column's on-disk string representation (hex vs.
            # hyphenated) isn't what this assertion is testing.
            raw = (await session.execute(text("SELECT admin_role FROM users"))).scalar_one()
            assert raw == "senior_moderator"  # lowercase .value, not "SENIOR_MODERATOR"

        # Fresh session -- proves the ORM round-trips it correctly too
        # (a genuinely NEW load, not the same in-memory object that set
        # it), not just that the raw column holds the right string. This
        # is the exact call shape (`session.get(User, ...)`) that raised
        # `LookupError` before the `values_callable` fix.
        async with db_session_factory() as session:
            reloaded = await session.get(User, user_id)
            assert reloaded.admin_role is AdminRole.SENIOR_MODERATOR

    async def test_admin_role_null_by_default(self, db_session_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
            assert user.admin_role is None


# ---------------------------------------------------------------------
# Tier hierarchy checks against the six already-confirmed-real routes.
# ---------------------------------------------------------------------


class TestModerationAccessTier:
    """`RequireModerationAccess` (Moderator rank or higher) gates
    `GET /admin/videos/review-queue`, `POST /admin/videos/{id}/override`,
    `GET /dashboards/admin`, `DELETE /chat/admin/bans/{user_id}`."""

    async def test_plain_student_403_on_review_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
        client = client_factory(student)
        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 403
        assert response.json()["detail"] == "Requires moderator access or higher."

    async def test_moderator_gets_200_on_review_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 200

    async def test_senior_moderator_gets_200_on_review_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 200

    async def test_owner_via_is_superuser_gets_200_on_review_queue(self, db_session_factory, client_factory):
        """The legacy `is_superuser=True` alias path -- proves the one
        pre-existing admin account from before this step keeps working
        with zero regression."""
        async with db_session_factory() as session:
            legacy_admin = await _make_user(session, is_superuser=True)
        client = client_factory(legacy_admin)
        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 200

    async def test_moderator_can_override_a_video(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            uploader = await _make_user(session, full_name="Uploader")
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "approved", "notes": "Looks fine."},
        )
        assert response.status_code == 200
        assert response.json()["new_status"] == "approved"

    async def test_moderator_can_read_admin_dashboard(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/dashboards/admin")
        assert response.status_code == 200


class TestSeniorModerationAccessTier:
    """`RequireSeniorModerationAccess` (Senior Moderator rank or higher)
    gates the two destructive actions: `DELETE /admin/videos/{id}` and
    `POST /admin/videos/{id}/reject-and-purge-media`. This is the tier
    boundary Step 1's own design decision is really about."""

    async def test_moderator_403_on_permanent_delete(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(moderator)
        response = client.delete(f"/api/v1/admin/videos/{video.id}")
        assert response.status_code == 403
        assert response.json()["detail"] == "Requires senior moderator access or higher."

        # Defense in depth -- the video must genuinely still exist.
        async with db_session_factory() as session:
            still_there = await session.get(StudentVideo, video.id)
            assert still_there is not None

    async def test_moderator_403_on_reject_and_purge(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.FAILED.value)

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/reject-and-purge-media", json={"notes": "spam"}
        )
        assert response.status_code == 403

    async def test_senior_moderator_can_permanently_delete(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(senior)
        response = client.delete(f"/api/v1/admin/videos/{video.id}")
        assert response.status_code == 204

        async with db_session_factory() as session:
            gone = await session.get(StudentVideo, video.id)
            assert gone is None

    async def test_senior_moderator_can_reject_and_purge(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.FAILED.value)

        client = client_factory(senior)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/reject-and-purge-media", json={"notes": "spam"}
        )
        assert response.status_code == 200
        assert response.json()["new_status"] == "rejected"


# ---------------------------------------------------------------------
# Owner-only role management.
# ---------------------------------------------------------------------


class TestOwnerOnlyRoleManagement:
    async def test_senior_moderator_403_on_role_assignment(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            target = await _make_user(session)

        client = client_factory(senior)
        response = client.patch(
            f"/api/v1/admin/users/{target.id}/role", json={"new_role": "moderator"}
        )
        assert response.status_code == 403
        assert response.json()["detail"] == "Requires owner access or higher."

    async def test_senior_moderator_403_on_team_list(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        response = client.get("/api/v1/admin/team")
        assert response.status_code == 403

    async def test_owner_can_grant_moderator(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, full_name="New Volunteer")

        client = client_factory(owner)
        response = client.patch(
            f"/api/v1/admin/users/{target.id}/role", json={"new_role": "moderator"}
        )
        assert response.status_code == 200
        body = response.json()
        assert body["user_id"] == str(target.id)
        assert body["admin_role"] == "moderator"

        async with db_session_factory() as session:
            persisted = await session.get(User, target.id)
            assert persisted.admin_role is AdminRole.MODERATOR
            # Confirmed real: this endpoint never touches is_superuser.
            assert persisted.is_superuser is False

    async def test_owner_can_revoke_role(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        response = client.patch(
            f"/api/v1/admin/users/{target.id}/role", json={"new_role": None}
        )
        assert response.status_code == 200
        assert response.json()["admin_role"] is None

        async with db_session_factory() as session:
            persisted = await session.get(User, target.id)
            assert persisted.admin_role is None

    async def test_owner_cannot_grant_owner_via_api(self, db_session_factory, client_factory):
        """The one-time-bootstrap-only guard, at the schema layer."""
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session)

        client = client_factory(owner)
        response = client.patch(
            f"/api/v1/admin/users/{target.id}/role", json={"new_role": "owner"}
        )
        assert response.status_code == 422

        async with db_session_factory() as session:
            persisted = await session.get(User, target.id)
            assert persisted.admin_role is None

    async def test_owner_cannot_change_own_role(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)

        client = client_factory(owner)
        response = client.patch(
            f"/api/v1/admin/users/{owner.id}/role", json={"new_role": "moderator"}
        )
        assert response.status_code == 400

    async def test_role_assignment_on_nonexistent_user_returns_404(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)

        client = client_factory(owner)
        response = client.patch(
            f"/api/v1/admin/users/{uuid.uuid4()}/role", json={"new_role": "moderator"}
        )
        assert response.status_code == 404

    async def test_owner_can_list_team(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER, full_name="Owner Person")
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Senior Person")
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Mod Person")
            # A plain student must NOT appear in the team list.
            await _make_user(session, full_name="Plain Student")

        client = client_factory(owner)
        response = client.get("/api/v1/admin/team")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3

        by_name = {item["full_name"]: item for item in body["items"]}
        assert set(by_name.keys()) == {"Owner Person", "Senior Person", "Mod Person"}
        assert by_name["Owner Person"]["effective_role"] == "owner"
        assert by_name["Owner Person"]["is_superuser"] is True
        assert by_name["Senior Person"]["effective_role"] == "senior_moderator"
        assert by_name["Mod Person"]["effective_role"] == "moderator"
