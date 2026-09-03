"""Behavioral tests for the Escalation Path Between Volunteers step:

- Tier gating: raising an escalation is Moderator-or-higher; resolving
  one is Senior-Moderator-or-higher only -- a plain Moderator who
  raised a flag cannot clear it themselves (this feature's own real
  point, see `admin_escalations.py`'s own module docstring).
- Idempotency: raising a second escalation on an already-escalated item
  returns the SAME row, doesn't stack a duplicate.
- Resolving an unescalated item is a harmless no-op (204), same
  "nothing to be unauthorized to release" posture the Claim System's
  own `release_claim` already established.
- A resolved escalation stops appearing in `GET /admin/escalations`,
  and a new escalation can be raised again afterward (not permanently
  stuck).
- A real `ModerationLogEntry` row is written for both escalate and
  resolve.
- Unknown item type / nonexistent item both real-404.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique
as `tests/test_moderation_claims.py`, whose table DDL/fixture helpers
this file reuses near-verbatim. `StudentVideo` is used as the real
underlying claimable/escalatable item throughout (one of the five real
`_CLAIMABLE_MODELS` entries `admin_escalations.py` reuses unchanged).
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
from app.models.enums import AdminRole
from app.models.moderation_escalation import ModerationEscalation
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import moderation_escalation_service

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
        await conn.run_sync(Base.metadata.create_all, tables=[ModerationEscalation.__table__])

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None, is_superuser: bool = False, full_name: str = "Test User") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name, admin_role=admin_role, is_superuser=is_superuser)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID) -> StudentVideo:
    video = StudentVideo(id=uuid.uuid4(), student_id=student_id, title="Test Video", status="pending_ai", ai_safety_flag=False)
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


def _escalate_url(video_id: uuid.UUID) -> str:
    return f"/api/v1/admin/moderation/student_video/{video_id}/escalate"


class TestEscalationTierGating:
    async def test_plain_moderator_can_raise(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.post(_escalate_url(video.id), json={"note": "Not sure this is safe, need a second opinion."})
        assert resp.status_code == 200

    async def test_plain_student_cannot_raise(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
        client = client_factory(student)
        resp = client.post(_escalate_url(video.id), json={"note": "..."})
        assert resp.status_code == 403

    async def test_plain_moderator_cannot_resolve_own_escalation(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        client.post(_escalate_url(video.id), json={"note": "flagging this"})
        resp = client.delete(_escalate_url(video.id))
        assert resp.status_code == 403

    async def test_senior_moderator_can_resolve(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(moderator)
        client.post(_escalate_url(video.id), json={"note": "flagging this"})

        client2 = client_factory(senior)
        resp = client2.delete(_escalate_url(video.id))
        assert resp.status_code == 204

    async def test_owner_can_resolve(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(moderator)
        client.post(_escalate_url(video.id), json={"note": "flagging this"})

        client2 = client_factory(owner)
        resp = client2.delete(_escalate_url(video.id))
        assert resp.status_code == 204

    async def test_unknown_item_type_404(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.post(f"/api/v1/admin/moderation/user/{uuid.uuid4()}/escalate", json={"note": "x"})
        assert resp.status_code == 404

    async def test_nonexistent_item_404(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.post(_escalate_url(uuid.uuid4()), json={"note": "x"})
        assert resp.status_code == 404


class TestEscalationBehavior:
    async def test_raised_escalation_has_real_fields(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Moderator One")
        client = client_factory(moderator)
        resp = client.post(_escalate_url(video.id), json={"note": "This looks like it might violate platform policy -- need a second opinion."})
        assert resp.status_code == 200
        body = resp.json()
        assert body["item_type"] == "student_video"
        assert body["item_id"] == str(video.id)
        assert body["note"] == "This looks like it might violate platform policy -- need a second opinion."
        assert body["escalated_by"] == str(moderator.id)
        assert body["escalated_by_name"] == "Moderator One"
        assert body["resolved_by"] is None
        assert body["resolved_at"] is None

    async def test_raising_twice_is_idempotent_same_row(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        r1 = client.post(_escalate_url(video.id), json={"note": "first reason"})
        r2 = client.post(_escalate_url(video.id), json={"note": "a different reason, should be ignored"})
        assert r1.status_code == 200 and r2.status_code == 200
        assert r1.json()["id"] == r2.json()["id"]
        assert r2.json()["note"] == "first reason"

        async with db_session_factory() as db:
            count = (await db.execute(select(ModerationEscalation))).scalars().all()
            assert len(count) == 1

    async def test_resolving_unescalated_item_is_noop(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        resp = client.delete(_escalate_url(video.id))
        assert resp.status_code == 204

    async def test_resolved_escalation_disappears_from_active_list_and_can_be_reraised(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)

        client = client_factory(moderator)
        client.post(_escalate_url(video.id), json={"note": "first flag"})

        senior_client = client_factory(senior)
        list_resp = senior_client.get("/api/v1/admin/escalations")
        assert list_resp.status_code == 200 and list_resp.json()["total"] == 1

        resolve_resp = senior_client.delete(_escalate_url(video.id))
        assert resolve_resp.status_code == 204

        list_resp2 = senior_client.get("/api/v1/admin/escalations")
        assert list_resp2.status_code == 200 and list_resp2.json()["total"] == 0

        # Re-raising after resolution creates a genuinely NEW active row.
        reraise_resp = client.post(_escalate_url(video.id), json={"note": "flagging again, new issue"})
        assert reraise_resp.status_code == 200
        list_resp3 = senior_client.get("/api/v1/admin/escalations")
        assert list_resp3.status_code == 200 and list_resp3.json()["total"] == 1
        assert list_resp3.json()["items"][0]["note"] == "flagging again, new issue"

    async def test_moderation_log_entries_written_for_escalate_and_resolve(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR)

        client = client_factory(moderator)
        client.post(_escalate_url(video.id), json={"note": "flag"})
        senior_client = client_factory(senior)
        senior_client.delete(_escalate_url(video.id))

        async with db_session_factory() as db:
            rows = (await db.execute(text("SELECT action FROM moderation_log_entries ORDER BY created_at"))).fetchall()
            actions = [r[0] for r in rows]
            assert actions == ["escalate", "escalation_resolve"]


class TestEscalationServiceUnit:
    async def test_get_active_escalation_none_when_unescalated(self, db_session_factory):
        from app.models.enums import ModerationTargetType

        async with db_session_factory() as db:
            result = await moderation_escalation_service.get_active_escalation(db, ModerationTargetType.STUDENT_VIDEO, uuid.uuid4())
            assert result is None
