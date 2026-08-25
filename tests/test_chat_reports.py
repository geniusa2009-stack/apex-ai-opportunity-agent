"""Behavioral tests for Extend Moderation to Chat Messages (Reporting +
Second Real Queue): `app/services/chat_report_service.py`'s real
get-or-create/anti-mass-report logic, the new
`POST /chat/rooms/{room_id}/messages/{message_id}/report` endpoint, the
Claim System's reuse for `ModerationTargetType.CHAT_MESSAGE_REPORT` (zero
new claim code -- `app/api/v1/endpoints/admin_claims.py`'s existing
`_CLAIMABLE_MODELS` entry), the new `admin_chat_reports.py` queue +
resolution endpoints, and this task's own real tier decision (dismiss/
escalate-ban = Moderator, delete-message = Senior Moderator).

Real HTTP layer via `TestClient` + `get_current_user`/`get_db` dependency
overrides, in-memory SQLite -- same established technique as
`tests/test_moderation_claims.py`. Redis (for `escalate_chat_report_to_ban`'s
real `chat_security.ban_user` call) is faked at the exact same
`_get_redis_client()` boundary `tests/test_chat.py` already established.
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
from app.models.chat import ChatMessage, ChatParticipant, ChatRoom
from app.models.chat_moderation import ChatMessageReport, ChatMessageReportEntry
from app.models.enums import AdminRole, ChatReportReason
from app.models.user import User
from app.services import (
    chat_report_service,
    chat_security,
    moderation_claim_service,
    notification_connection_manager,
    notification_service,
)

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

_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY,
    actor_id VARCHAR(36),
    actor_role VARCHAR(20) NOT NULL,
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(30) NOT NULL,
    target_id VARCHAR(36) NOT NULL,
    outcome TEXT NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Transparency: Notify Users of Moderation Actions addition -- both
# `delete_reported_message` and `escalate_chat_report_to_ban` now call
# `notification_service.create_notification`, which was previously never
# exercised by this file's own fixture. Same raw-SQL/JSONB-as-TEXT
# pattern this codebase already uses for this table everywhere else it's
# needed (`tests/test_admin_moderation.py`, `tests/test_repeat_offenders.py`).
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
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                GamificationProfile.__table__,
                Badge.__table__,
                UserBadge.__table__,
                ChatRoom.__table__,
                ChatParticipant.__table__,
                ChatMessage.__table__,
                ChatMessageReport.__table__,
                ChatMessageReportEntry.__table__,
            ],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession, *, is_superuser: bool = False, admin_role: AdminRole | None = None, full_name: str = "Test User"
) -> User:
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name,
        is_superuser=is_superuser, admin_role=admin_role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_room_with_participants(db: AsyncSession, *user_ids: uuid.UUID) -> ChatRoom:
    room = ChatRoom(id=uuid.uuid4(), is_group=len(user_ids) > 2, name="Room" if len(user_ids) > 2 else None)
    db.add(room)
    await db.flush()
    for uid in user_ids:
        db.add(ChatParticipant(room_id=room.id, user_id=uid))
    await db.commit()
    await db.refresh(room)
    return room


async def _make_message(db: AsyncSession, *, room_id: uuid.UUID, sender_id: uuid.UUID, content: str = "hey") -> ChatMessage:
    message = ChatMessage(id=uuid.uuid4(), room_id=room_id, sender_id=sender_id, content=content)
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


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


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(chat_security, "_get_redis_client", lambda: redis)
    return redis


@pytest.fixture(autouse=True)
def _fake_notification_broadcast(monkeypatch):
    """Transparency: Notify Users of Moderation Actions addition -- both
    `delete_reported_message` and `escalate_chat_report_to_ban` now call
    `notification_service.create_notification`, which both publishes a
    real-time event and (for `type="WARNING"`, the type both call sites
    use) dispatches a real Celery email task. Neither `redis://redis:6379`
    nor a Celery broker is reachable in this sandbox; both are already
    wrapped in `create_notification`'s own best-effort try/except, but
    the real DNS-resolution timeout on each costs several real seconds.
    Patched at the same boundaries those two modules already isolate,
    same posture as `fake_redis` above for `chat_security`'s own client.
    """

    class _NoopPublishClient:
        async def publish(self, channel: str, message: str) -> None:
            return None

    class _NoopEmailTask:
        def delay(self, *args, **kwargs) -> None:
            return None

    monkeypatch.setattr(notification_connection_manager, "_get_redis_client", lambda: _NoopPublishClient())
    monkeypatch.setattr(notification_service, "send_transactional_email_task", _NoopEmailTask())


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


def _report_url(room_id: uuid.UUID, message_id: uuid.UUID) -> str:
    return f"/api/v1/chat/rooms/{room_id}/messages/{message_id}/report"


def _claim_url(report_id: uuid.UUID) -> str:
    return f"/api/v1/admin/moderation/chat_message_report/{report_id}/claim"


# ---------------------------------------------------------------------
# Service-layer: create_report (get-or-create case + anti-mass-report)
# ---------------------------------------------------------------------


class TestChatReportServiceUnit:
    pytestmark = pytest.mark.asyncio

    async def test_first_report_creates_a_new_open_case(self, db_session_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

            case, entry, total = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()
            assert case.message_id == message.id
            assert case.status == "open"
            assert entry.reporter_id == reporter.id
            assert total == 1

    async def test_second_distinct_reporter_reuses_the_same_open_case(self, db_session_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter_a = await _make_user(session)
            reporter_b = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id, reporter_a.id, reporter_b.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

            case_a, _, total_a = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter_a.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()
            case_b, _, total_b = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter_b.id, reason=ChatReportReason.HARASSMENT, details="context"
            )
            await session.commit()

            assert case_a.id == case_b.id
            assert total_a == 1
            assert total_b == 2

    async def test_same_reporter_reporting_twice_raises_duplicate(self, db_session_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

            await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

            with pytest.raises(chat_report_service.DuplicateReportError):
                await chat_report_service.create_report(
                    session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
                )

    async def test_self_report_raises(self, db_session_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

            with pytest.raises(chat_report_service.SelfReportError):
                await chat_report_service.create_report(
                    session, message=message, reporter_id=sender.id, reason=ChatReportReason.SPAM, details=None
                )


# ---------------------------------------------------------------------
# Real HTTP: POST .../report
# ---------------------------------------------------------------------


class TestReportEndpoint:
    pytestmark = pytest.mark.asyncio

    async def test_participant_can_report_another_users_message(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

        client = client_factory(reporter)
        response = client.post(_report_url(room.id, message.id), json={"reason": "spam"})
        assert response.status_code == 201
        body = response.json()
        assert body["message_id"] == str(message.id)
        assert body["reason"] == "spam"
        assert body["total_reports"] == 1

    async def test_self_report_is_400(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

        client = client_factory(sender)
        response = client.post(_report_url(room.id, message.id), json={"reason": "spam"})
        assert response.status_code == 400

    async def test_duplicate_report_by_same_user_is_409(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

        client = client_factory(reporter)
        first = client.post(_report_url(room.id, message.id), json={"reason": "spam"})
        second = client.post(_report_url(room.id, message.id), json={"reason": "harassment"})
        assert first.status_code == 201
        assert second.status_code == 409

    async def test_non_participant_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            outsider = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)

        client = client_factory(outsider)
        response = client.post(_report_url(room.id, message.id), json={"reason": "spam"})
        assert response.status_code == 403

    async def test_reporting_a_nonexistent_message_404s(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            reporter = await _make_user(session)
            room = await _make_room_with_participants(session, reporter.id)

        client = client_factory(reporter)
        response = client.post(_report_url(room.id, uuid.uuid4()), json={"reason": "spam"})
        assert response.status_code == 404


# ---------------------------------------------------------------------
# Claim System reuse: chat_message_report is claimable with ZERO new
# claim code, via the existing admin_claims.py endpoints.
# ---------------------------------------------------------------------


class TestClaimReuseForChatReports:
    pytestmark = pytest.mark.asyncio

    async def test_moderator_can_claim_a_chat_report(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Claim Holder")
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

        client = client_factory(moderator)
        response = client.post(_claim_url(case.id))
        assert response.status_code == 200
        body = response.json()
        assert body["item_type"] == "chat_message_report"
        assert body["claimed_by"] == str(moderator.id)
        assert body["claimed_by_name"] == "Claim Holder"

    async def test_second_moderator_gets_409_on_actively_claimed_report(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            mod_a = await _make_user(session, admin_role=AdminRole.MODERATOR)
            mod_b = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

        client_factory(mod_a).post(_claim_url(case.id))
        response = client_factory(mod_b).post(_claim_url(case.id))
        assert response.status_code == 409

    async def test_release_by_claimant_succeeds(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

        client = client_factory(moderator)
        client.post(_claim_url(case.id))
        response = client.delete(_claim_url(case.id))
        assert response.status_code == 204


# ---------------------------------------------------------------------
# Real HTTP: GET /admin/chat/reports queue
# ---------------------------------------------------------------------


class TestChatReportsQueue:
    pytestmark = pytest.mark.asyncio

    async def test_queue_shows_real_message_content_and_report_entries(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session, full_name="Sender Person")
            reporter = await _make_user(session, full_name="Reporter Person")
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id, content="spammy link here")
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details="posted a scam link"
            )
            await session.commit()

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/chat/reports")
        assert response.status_code == 200
        items = response.json()["items"]
        item = next(i for i in items if i["id"] == str(case.id))
        assert item["message"]["content"] == "spammy link here"
        assert item["message"]["sender_name"] == "Sender Person"
        assert item["report_count"] == 1
        assert item["reports"][0]["reporter_name"] == "Reporter Person"
        assert item["reports"][0]["reason"] == "spam"
        assert item["status"] == "open"

    async def test_plain_student_gets_403_on_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
        client = client_factory(student)
        response = client.get("/api/v1/admin/chat/reports")
        assert response.status_code == 403


# ---------------------------------------------------------------------
# Real HTTP: resolution actions + tier gating
# ---------------------------------------------------------------------


class TestDismissAction:
    pytestmark = pytest.mark.asyncio

    async def test_moderator_can_dismiss(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

        client = client_factory(moderator)
        response = client.post(f"/api/v1/admin/chat/reports/{case.id}/dismiss", json={"notes": "not real spam"})
        assert response.status_code == 200
        assert response.json()["status"] == "dismissed"

    async def test_dismissing_twice_is_409(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

        client = client_factory(moderator)
        client.post(f"/api/v1/admin/chat/reports/{case.id}/dismiss", json={})
        second = client.post(f"/api/v1/admin/chat/reports/{case.id}/dismiss", json={})
        assert second.status_code == 409

    async def test_plain_student_gets_403_on_dismiss(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            student = await _make_user(session)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

        client = client_factory(student)
        response = client.post(f"/api/v1/admin/chat/reports/{case.id}/dismiss", json={})
        assert response.status_code == 403


class TestDeleteMessageAction:
    pytestmark = pytest.mark.asyncio

    async def test_plain_moderator_gets_403_on_delete_message(self, db_session_factory, client_factory):
        """The real tier decision this task requires: deleting the
        message is IRREVERSIBLE (no undo endpoint exists), so it needs
        Senior Moderator, same bucket as `delete_video_permanently`."""
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.HARASSMENT, details=None
            )
            await session.commit()

        client = client_factory(moderator)
        response = client.delete(f"/api/v1/admin/chat/reports/{case.id}/message")
        assert response.status_code == 403

    async def test_senior_moderator_can_delete_message(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.HARASSMENT, details=None
            )
            await session.commit()
            message_id = message.id

        client = client_factory(senior_mod)
        response = client.delete(f"/api/v1/admin/chat/reports/{case.id}/message")
        assert response.status_code == 200
        assert response.json()["status"] == "message_deleted"

        async with db_session_factory() as session:
            deleted = await session.get(ChatMessage, message_id)
            assert deleted is None


class TestEscalateBanAction:
    pytestmark = pytest.mark.asyncio

    async def test_moderator_can_escalate_to_a_real_ban(self, db_session_factory, client_factory, fake_redis):
        """Reversible via the existing unban endpoint, same tier as
        `override_video_decision`/`admin_unban_user` -- Moderator, not
        Senior Moderator."""
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.HARASSMENT, details=None
            )
            await session.commit()
            sender_id = sender.id

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/chat/reports/{case.id}/escalate-ban",
            json={"notes": "repeat harassment", "duration_seconds": 900},
        )
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "escalated_ban"
        assert body["banned_until_seconds"] == 900
        assert await chat_security.is_user_banned(sender_id) is True

    async def test_escalating_twice_is_409(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = await _make_room_with_participants(session, sender.id, reporter.id)
            message = await _make_message(session, room_id=room.id, sender_id=sender.id)
            case, _, _ = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.HARASSMENT, details=None
            )
            await session.commit()

        client = client_factory(moderator)
        client.post(f"/api/v1/admin/chat/reports/{case.id}/escalate-ban", json={})
        second = client.post(f"/api/v1/admin/chat/reports/{case.id}/escalate-ban", json={})
        assert second.status_code == 409
