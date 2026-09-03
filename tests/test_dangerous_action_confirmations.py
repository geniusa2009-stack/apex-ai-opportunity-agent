"""Behavioral tests for Two-Person Confirmation for Dangerous Actions.

- The pending-then-confirm flow: `DELETE /admin/videos/{id}` (and the
  other three real irreversible actions) no longer execute directly --
  they create a real pending `DangerousActionConfirmation`, and the
  underlying mutation only happens once a SECOND, distinct Senior
  Moderator (or Owner) approves it.
- REJECTION OF SELF-APPROVAL -- the central rule this task requires:
  the requester cannot also be the approver.
- The Owner-override path, and its DISTINCT logging
  (`dangerous_action_overridden`, never `dangerous_action_approved`).
- `CHAT_MESSAGE_DELETE`/`GUILD_CONTENT_DELETE` (the other two real
  dangerous actions) are covered by `tests/test_chat_reports.py`/
  `tests/test_guild_reports.py`'s OWN updated tests (each domain's test
  file owns its own action's coverage through this same gate) -- this
  file focuses on the two video actions plus the generic mechanism
  itself (self-approval, idempotency, override, tier gating, listing),
  which is domain-agnostic and does not need chat/guild fixtures.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique
`tests/test_moderation_escalations.py` uses, whose table DDL/fixture
helpers this file reuses near-verbatim.
"""

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
from app.models.dangerous_action_confirmation import DangerousActionConfirmation
from app.models.enums import AdminRole
from app.models.moderation_log import ModerationLogEntry
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

_CREATE_NOTIFICATIONS_TABLE_SQL = """
CREATE TABLE notifications (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    type VARCHAR(20) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    payload TEXT,
    is_read BOOLEAN NOT NULL DEFAULT 0,
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
        await conn.run_sync(Base.metadata.create_all, tables=[DangerousActionConfirmation.__table__])

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None, is_superuser: bool = False, full_name: str = "Test User") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name, admin_role=admin_role, is_superuser=is_superuser)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str = "approved") -> StudentVideo:
    video = StudentVideo(id=uuid.uuid4(), student_id=student_id, title="Test Video", status=status, ai_safety_flag=False)
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


def _delete_url(video_id: uuid.UUID) -> str:
    return f"/api/v1/admin/videos/{video_id}"


def _approve_url(confirmation_id) -> str:
    return f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"


def _override_url(item_type: str, item_id, action: str) -> str:
    return f"/api/v1/admin/moderation/{item_type}/{item_id}/dangerous-actions/{action}/override"


async def _fetch_log_actions(db_session_factory) -> list[str]:
    async with db_session_factory() as db:
        result = await db.execute(select(ModerationLogEntry.action))
        return list(result.scalars().all())


class TestDangerousActionTierGating:
    async def test_moderator_cannot_even_request(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.delete(_delete_url(video.id))
        assert resp.status_code == 403

        async with db_session_factory() as db:
            still_there = await db.get(StudentVideo, video.id)
            assert still_there is not None

    async def test_senior_moderator_request_creates_a_real_pending_row(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        resp = client.delete(_delete_url(video.id))
        assert resp.status_code == 202
        body = resp.json()
        assert body["status"] == "pending"
        assert body["action"] == "video_delete"
        assert body["item_type"] == "student_video"
        assert body["item_id"] == str(video.id)

        async with db_session_factory() as db:
            still_there = await db.get(StudentVideo, video.id)
            assert still_there is not None, "the video must NOT be deleted by the request alone"

    async def test_moderator_cannot_approve(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(senior)
        resp = client.delete(_delete_url(video.id))
        confirmation_id = resp.json()["confirmation_id"]

        mod_client = client_factory(moderator)
        approve_resp = mod_client.post(_approve_url(confirmation_id))
        assert approve_resp.status_code == 403

    async def test_requester_cannot_approve_their_own_request(self, client_factory, db_session_factory):
        """THE central rule this task requires: a real two-person
        control, not a single Senior Moderator clicking twice."""
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        resp = client.delete(_delete_url(video.id))
        confirmation_id = resp.json()["confirmation_id"]

        # Same account, second call.
        self_approve_resp = client.post(_approve_url(confirmation_id))
        assert self_approve_resp.status_code == 403
        assert "cannot also approve" in self_approve_resp.json()["detail"]

        async with db_session_factory() as db:
            still_there = await db.get(StudentVideo, video.id)
            assert still_there is not None

    async def test_owner_requesting_still_needs_a_distinct_second_approver(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        resp = client.delete(_delete_url(video.id))
        confirmation_id = resp.json()["confirmation_id"]

        self_approve_resp = client.post(_approve_url(confirmation_id))
        assert self_approve_resp.status_code == 403

    async def test_a_different_senior_moderator_can_approve(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Requester")
            second_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Approver")
        client = client_factory(senior)
        resp = client.delete(_delete_url(video.id))
        confirmation_id = resp.json()["confirmation_id"]

        approver_client = client_factory(second_senior)
        approve_resp = approver_client.post(_approve_url(confirmation_id))
        assert approve_resp.status_code == 200
        assert approve_resp.json()["status"] == "executed"
        assert approve_resp.json()["is_override"] is False

        async with db_session_factory() as db:
            assert await db.get(StudentVideo, video.id) is None

    async def test_approving_an_already_executed_confirmation_is_409(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
            second_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
            third_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        resp = client.delete(_delete_url(video.id))
        confirmation_id = resp.json()["confirmation_id"]

        approver_client = client_factory(second_senior)
        first_approve = approver_client.post(_approve_url(confirmation_id))
        assert first_approve.status_code == 200

        third_client = client_factory(third_senior)
        second_approve = third_client.post(_approve_url(confirmation_id))
        assert second_approve.status_code == 409

    async def test_approving_a_nonexistent_confirmation_404s(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        resp = client.post(_approve_url(uuid.uuid4()))
        assert resp.status_code == 404


class TestDangerousActionRequestIdempotency:
    async def test_requesting_twice_before_approval_returns_the_same_confirmation(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        first = client.delete(_delete_url(video.id))
        second = client.delete(_delete_url(video.id))
        assert first.status_code == 202
        assert second.status_code == 202
        assert first.json()["confirmation_id"] == second.json()["confirmation_id"]

        async with db_session_factory() as db:
            result = await db.execute(select(DangerousActionConfirmation))
            rows = result.scalars().all()
            assert len(rows) == 1, "no duplicate pending row for the same still-pending action"


class TestDangerousActionOwnerOverride:
    async def test_owner_override_with_no_prior_request_executes_immediately(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        resp = client.post(_override_url("student_video", video.id, "video_delete"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["status"] == "executed"
        assert body["is_override"] is True

        async with db_session_factory() as db:
            assert await db.get(StudentVideo, video.id) is None

        actions = await _fetch_log_actions(db_session_factory)
        assert "video_delete" in actions
        assert "dangerous_action_overridden" in actions
        assert "dangerous_action_approved" not in actions, "an override must never be logged as a normal approval"

    async def test_owner_override_of_an_existing_pending_request_reuses_it(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Requester")
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(senior)
        request_resp = client.delete(_delete_url(video.id))
        confirmation_id = request_resp.json()["confirmation_id"]

        owner_client = client_factory(owner)
        override_resp = owner_client.post(_override_url("student_video", video.id, "video_delete"))
        assert override_resp.status_code == 200
        assert override_resp.json()["id"] == confirmation_id
        assert override_resp.json()["is_override"] is True

        async with db_session_factory() as db:
            result = await db.execute(select(DangerousActionConfirmation))
            rows = result.scalars().all()
            assert len(rows) == 1, "the original pending row is reused, not duplicated"
            assert rows[0].requested_by == senior.id, "the real original requester stays attributed"
            assert rows[0].executed_by == owner.id

    async def test_senior_moderator_cannot_override_owner_only(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        resp = client.post(_override_url("student_video", video.id, "video_delete"))
        assert resp.status_code == 403

    async def test_override_action_item_type_mismatch_is_400(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        # `chat_message_delete` applies to `chat_message_report` items, not `student_video`.
        resp = client.post(_override_url("student_video", video.id, "chat_message_delete"))
        assert resp.status_code == 400


class TestDangerousActionListing:
    async def test_pending_list_shows_real_requests_oldest_first(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video1 = await _make_video(db, student_id=student.id)
            video2 = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        client.delete(_delete_url(video1.id))
        client.delete(_delete_url(video2.id))

        resp = client.get("/api/v1/admin/moderation/dangerous-actions")
        assert resp.status_code == 200
        body = resp.json()
        assert body["total"] == 2
        assert [item["item_id"] for item in body["items"]] == [str(video1.id), str(video2.id)]

    async def test_executed_confirmations_do_not_appear_in_the_pending_list(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
            second_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        confirmation_id = client.delete(_delete_url(video.id)).json()["confirmation_id"]
        client_factory(second_senior).post(_approve_url(confirmation_id))

        resp = client.get("/api/v1/admin/moderation/dangerous-actions")
        assert resp.json()["total"] == 0

    async def test_moderator_cannot_view_the_pending_queue(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.get("/api/v1/admin/moderation/dangerous-actions")
        assert resp.status_code == 403


class TestDangerousActionRejectAndPurge:
    """A second, distinct real dangerous action -- confirms the SAME
    gate mechanism (not something special-cased to `video_delete`)
    genuinely covers `video_reject_and_purge` too."""

    async def test_full_flow_keeps_the_row_but_purges_media_only_after_approval(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id, status="failed")
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
            second_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)

        client = client_factory(senior)
        resp = client.post(
            f"/api/v1/admin/videos/{video.id}/reject-and-purge-media", json={"notes": "policy violation"}
        )
        assert resp.status_code == 202
        confirmation_id = resp.json()["confirmation_id"]

        async with db_session_factory() as db:
            still_pending = await db.get(StudentVideo, video.id)
            assert still_pending.status == "failed", "not yet rejected -- still awaiting second approval"

        approver_client = client_factory(second_senior)
        approve_resp = approver_client.post(_approve_url(confirmation_id))
        assert approve_resp.status_code == 200

        async with db_session_factory() as db:
            video_row = await db.get(StudentVideo, video.id)
            assert video_row.status == "rejected"
            assert video_row.admin_notes == "policy violation"
