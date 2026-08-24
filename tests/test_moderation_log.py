"""Behavioral tests for the Automatic Moderation Activity Log step:
`app/services/moderation_log_service.py`'s real write path, its wiring
into the four real write actions (`override_video_decision`,
`delete_video_permanently`, `reject_video_and_purge_media` in
`admin_moderation.py`; `admin_unban_user` in `chat.py`), and the two new
Owner-only read endpoints (`app/api/v1/endpoints/admin_activity_log.py`).

Real HTTP layer, via `TestClient` + `get_current_user`/`get_db`
dependency overrides -- same technique as `tests/test_admin_roles.py`.
In-memory SQLite, `StaticPool`. Same table shape as `test_admin_roles.py`
(`users`/`user_bookmarks`/`student_videos`/`sources`/`opportunities`/
`notifications`), PLUS `moderation_log_entries` (raw SQL -- this model's
`outcome` column is `JSONB`, same "SQLite can't compile JSONB via
`Base.metadata.create_all`" reason `notifications.payload` needed a raw
table).
"""

import json
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
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType, StudentVideoStatus
from app.models.moderation_log import ModerationLogEntry
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import chat_security

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
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
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

# `outcome` as plain `TEXT` (JSON-serialized by hand in `_make_log_entry`
# below) -- same "JSONB has no SQLite DDL compiler support" reason as
# `notifications.payload` elsewhere in this codebase's own test suite.
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


async def _make_log_entry(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    actor_role: AdminRole,
    action: ModerationAction,
    target_type: ModerationTargetType,
    target_id: uuid.UUID,
    outcome: dict,
    created_at: str | None = None,
) -> None:
    """Raw-SQL seed helper (not the ORM `ModerationLogEntry` -- SQLite
    can't compile its real `JSONB` column type via the ORM the way
    Postgres can) -- used ONLY for pagination/read-endpoint seed data
    where many entries are needed quickly; the four real write-action
    tests below go through the REAL HTTP endpoints instead, so they
    exercise the real `moderation_log_service.log_action` call sites,
    not this helper.
    """
    await db.execute(
        text(
            """INSERT INTO moderation_log_entries
            (id, actor_id, actor_role, action, target_type, target_id, outcome, created_at)
            VALUES (:id, :actor_id, :actor_role, :action, :target_type, :target_id, :outcome,
                    COALESCE(:created_at, CURRENT_TIMESTAMP))"""
        ),
        {
            # `.hex` (32-char, no dashes), NOT `str()` (dashed) -- matches
            # the REAL storage format `UUID(as_uuid=True)` actually uses
            # once compiled against SQLite (confirmed empirically: the
            # real write-action tests above showed rows written through
            # the real HTTP endpoints come back dash-free from a raw
            # `SELECT`). A dashed seed value here would silently never
            # match the ORM's own `WHERE actor_id = :actor_id` filter.
            "id": uuid.uuid4().hex,
            "actor_id": actor_id.hex,
            "actor_role": actor_role.value,
            "action": action.value,
            "target_type": target_type.value,
            "target_id": target_id.hex,
            "outcome": json.dumps(outcome),
            "created_at": created_at,
        },
    )
    await db.commit()


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


class _FakeRedis:
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


async def _fetch_all_log_rows(db_session_factory) -> list[dict]:
    async with db_session_factory() as session:
        rows = (
            await session.execute(
                text("SELECT actor_id, actor_role, action, target_type, target_id, outcome FROM moderation_log_entries")
            )
        ).all()
    return [
        {
            "actor_id": r.actor_id,
            "actor_role": r.actor_role,
            "action": r.action,
            "target_type": r.target_type,
            "target_id": r.target_id,
            "outcome": json.loads(r.outcome),
        }
        for r in rows
    ]


# ---------------------------------------------------------------------
# The four real write actions each log automatically.
# ---------------------------------------------------------------------


class TestAutomaticLogging:
    async def test_video_override_logs_a_real_entry(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "approved", "notes": "looks fine"},
        )
        assert response.status_code == 200

        rows = await _fetch_all_log_rows(db_session_factory)
        assert len(rows) == 1
        entry = rows[0]
        # `.hex`, not `str()` -- raw-SQL reads see the REAL storage
        # format `UUID(as_uuid=True)` uses once compiled against SQLite
        # (no dashes); see `_make_log_entry`'s own comment for the
        # empirical confirmation.
        assert entry["actor_id"] == moderator.id.hex
        assert entry["actor_role"] == "moderator"
        assert entry["action"] == "video_override"
        assert entry["target_type"] == "student_video"
        assert entry["target_id"] == video.id.hex
        assert entry["outcome"]["new_status"] == "approved"
        assert entry["outcome"]["notes"] == "looks fine"

    async def test_video_delete_logs_a_real_entry(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(senior)
        response = client.delete(f"/api/v1/admin/videos/{video.id}")
        assert response.status_code == 204

        rows = await _fetch_all_log_rows(db_session_factory)
        assert len(rows) == 1
        entry = rows[0]
        assert entry["actor_id"] == senior.id.hex
        assert entry["actor_role"] == "senior_moderator"
        assert entry["action"] == "video_delete"
        assert entry["target_type"] == "student_video"
        assert entry["target_id"] == video.id.hex
        assert entry["outcome"]["video_deleted"] is True
        assert entry["outcome"]["student_id"] == str(uploader.id)

    async def test_reject_and_purge_logs_a_real_entry(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.FAILED.value)

        client = client_factory(senior)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/reject-and-purge-media", json={"notes": "spam"}
        )
        assert response.status_code == 200

        rows = await _fetch_all_log_rows(db_session_factory)
        assert len(rows) == 1
        entry = rows[0]
        assert entry["actor_role"] == "senior_moderator"
        assert entry["action"] == "video_reject_and_purge"
        assert entry["target_id"] == video.id.hex
        assert entry["outcome"]["new_status"] == "rejected"

    async def test_chat_unban_logs_a_real_entry(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        target_user_id = uuid.uuid4()
        await chat_security.ban_user(target_user_id, duration_seconds=900)

        client = client_factory(moderator)
        response = client.delete(f"/api/v1/chat/admin/bans/{target_user_id}")
        assert response.status_code == 200
        assert response.json()["was_banned"] is True

        rows = await _fetch_all_log_rows(db_session_factory)
        assert len(rows) == 1
        entry = rows[0]
        assert entry["actor_id"] == moderator.id.hex
        assert entry["actor_role"] == "moderator"
        assert entry["action"] == "chat_unban"
        assert entry["target_type"] == "user"
        assert entry["target_id"] == target_user_id.hex
        assert entry["outcome"]["was_banned"] is True

    async def test_chat_unban_of_non_banned_user_still_logs(self, db_session_factory, client_factory, fake_redis):
        """DISCLOSED DESIGN CHECK: logging happens regardless of whether
        the target was actually banned -- the outcome field is where
        that nuance lives, not whether a log entry exists at all."""
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(moderator)
        response = client.delete(f"/api/v1/chat/admin/bans/{uuid.uuid4()}")
        assert response.status_code == 200
        assert response.json()["was_banned"] is False

        rows = await _fetch_all_log_rows(db_session_factory)
        assert len(rows) == 1
        assert rows[0]["outcome"]["was_banned"] is False

    async def test_legacy_superuser_actor_role_logs_as_owner(self, db_session_factory, client_factory):
        """A legacy `is_superuser=True` account (no `admin_role` set)
        must log with `actor_role="owner"` -- the resolved EFFECTIVE
        tier, not the raw (possibly null) column. Proves
        `effective_admin_role` is what's actually wired in, not
        `current_user.admin_role` directly."""
        async with db_session_factory() as session:
            legacy_admin = await _make_user(session, is_superuser=True, admin_role=None)
            uploader = await _make_user(session)
            video = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(legacy_admin)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "rejected"}
        )
        assert response.status_code == 200

        rows = await _fetch_all_log_rows(db_session_factory)
        assert rows[0]["actor_role"] == "owner"

    async def test_review_queue_view_does_not_log(self, db_session_factory, client_factory):
        """Read-only action -- this step's own explicit "review-queue-view
        doesn't need logging" instruction."""
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 200

        rows = await _fetch_all_log_rows(db_session_factory)
        assert rows == []


# ---------------------------------------------------------------------
# Role-at-time-of-action does not drift with later role changes.
# ---------------------------------------------------------------------


class TestRoleAtTimeOfAction:
    async def test_old_entries_keep_the_old_role_after_a_promotion(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            volunteer = await _make_user(session, admin_role=AdminRole.MODERATOR)
            uploader = await _make_user(session)
            video_one = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.REJECTED.value)

        # Act once as MODERATOR.
        client = client_factory(volunteer)
        resp1 = client.post(
            f"/api/v1/admin/videos/{video_one.id}/override", json={"target_status": "approved"}
        )
        assert resp1.status_code == 200

        # Promote to SENIOR_MODERATOR directly in the DB (simulating an
        # Owner's real `PATCH /admin/users/{id}/role` call from Phase 2 --
        # that endpoint's own behavior is already covered by
        # tests/test_admin_roles.py; this test's own focus is the LOG's
        # behavior across the change, not the promotion mechanism).
        async with db_session_factory() as session:
            fresh = await session.get(User, volunteer.id)
            fresh.admin_role = AdminRole.SENIOR_MODERATOR
            await session.commit()

        # Act again, now as SENIOR_MODERATOR, on a DIFFERENT video (a
        # senior-only action, to also prove the new tier is honored for
        # real, not just remembered from before promotion).
        async with db_session_factory() as session:
            video_two = await _make_video(session, student_id=uploader.id, status=StudentVideoStatus.FAILED.value)
        client2 = client_factory(volunteer)
        resp2 = client2.delete(f"/api/v1/admin/videos/{video_two.id}")
        assert resp2.status_code == 204

        rows = await _fetch_all_log_rows(db_session_factory)
        assert len(rows) == 2
        by_action = {r["action"]: r for r in rows}
        # The FIRST action's own log entry must still say "moderator" --
        # this is the crux of this step's own "capture it at
        # time-of-action... the historical record shouldn't drift"
        # requirement.
        assert by_action["video_override"]["actor_role"] == "moderator"
        assert by_action["video_delete"]["actor_role"] == "senior_moderator"


# ---------------------------------------------------------------------
# Owner-only read endpoints + real pagination.
# ---------------------------------------------------------------------


class TestActivityLogReadEndpoints:
    async def test_moderator_403_on_actor_log(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get(f"/api/v1/admin/activity-log/{moderator.id}")
        assert response.status_code == 403
        assert response.json()["detail"] == "Requires owner access or higher."

    async def test_senior_moderator_403_on_team_log(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        response = client.get("/api/v1/admin/activity-log")
        assert response.status_code == 403

    async def test_owner_can_read_actor_log_with_denormalized_fields(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Mod Person")
            video_id = uuid.uuid4()
            await _make_log_entry(
                session,
                actor_id=moderator.id,
                actor_role=AdminRole.MODERATOR,
                action=ModerationAction.VIDEO_OVERRIDE,
                target_type=ModerationTargetType.STUDENT_VIDEO,
                target_id=video_id,
                outcome={"new_status": "approved"},
            )

        client = client_factory(owner)
        response = client.get(f"/api/v1/admin/activity-log/{moderator.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["actor_id"] == str(moderator.id)
        assert item["actor_full_name"] == "Mod Person"
        assert item["actor_role"] == "moderator"
        assert item["action"] == "video_override"
        assert item["target_id"] == str(video_id)
        assert item["outcome"] == {"new_status": "approved"}

    async def test_actor_log_only_shows_that_actors_entries(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            mod_a = await _make_user(session, admin_role=AdminRole.MODERATOR)
            mod_b = await _make_user(session, admin_role=AdminRole.MODERATOR)
            for _ in range(3):
                await _make_log_entry(
                    session, actor_id=mod_a.id, actor_role=AdminRole.MODERATOR,
                    action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                    target_id=uuid.uuid4(), outcome={"was_banned": True},
                )
            await _make_log_entry(
                session, actor_id=mod_b.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=uuid.uuid4(), outcome={"was_banned": True},
            )

        client = client_factory(owner)
        response = client.get(f"/api/v1/admin/activity-log/{mod_a.id}")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3
        assert all(item["actor_id"] == str(mod_a.id) for item in body["items"])

    async def test_team_log_pagination_with_more_than_one_page(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            # 25 entries -- more than one page at the default limit=20.
            for i in range(25):
                await _make_log_entry(
                    session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                    action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                    target_id=uuid.uuid4(), outcome={"seq": i},
                    created_at=f"2026-01-01 00:00:{i:02d}",
                )

        client = client_factory(owner)
        page1 = client.get("/api/v1/admin/activity-log")
        assert page1.status_code == 200
        body1 = page1.json()
        assert body1["total"] == 25
        assert len(body1["items"]) == 20
        assert body1["limit"] == 20
        assert body1["offset"] == 0
        # Newest first -- seq 24 (created last) should lead.
        assert body1["items"][0]["outcome"]["seq"] == 24

        page2 = client.get("/api/v1/admin/activity-log?offset=20")
        assert page2.status_code == 200
        body2 = page2.json()
        assert body2["total"] == 25
        assert len(body2["items"]) == 5
        assert body2["offset"] == 20
        # No overlap between the two pages.
        ids_page1 = {item["id"] for item in body1["items"]}
        ids_page2 = {item["id"] for item in body2["items"]}
        assert ids_page1.isdisjoint(ids_page2)

    async def test_team_log_date_range_filter(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=uuid.uuid4(), outcome={"tag": "old"}, created_at="2020-01-01 00:00:00",
            )
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=uuid.uuid4(), outcome={"tag": "recent"}, created_at="2026-06-01 00:00:00",
            )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log?start=2026-01-01T00:00:00")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        assert body["items"][0]["outcome"]["tag"] == "recent"

    async def test_actor_log_empty_for_user_with_no_entries(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get(f"/api/v1/admin/activity-log/{uuid.uuid4()}")
        assert response.status_code == 200
        assert response.json() == {"items": [], "total": 0, "limit": 20, "offset": 0}
