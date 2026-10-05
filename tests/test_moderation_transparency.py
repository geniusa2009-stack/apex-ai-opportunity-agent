"""Behavioral tests for "Transparency: Notify Users of Moderation Actions +
Real Appeal Path": the six real notification call sites this task adds
(video override-reject, video reject-and-purge, video permanent-delete,
chat message delete, chat escalate-ban -- the automatic Arabic-filter
CHAT_AUTO_BAN notification is covered separately in `tests/test_chat.py`,
alongside its own existing WebSocket integration fixture), the real
appeal submission/resolution endpoints (`app/api/v1/endpoints/
moderation_appeals.py`), tier gating, and the per-action-type reversal
semantics (`app/services/moderation_appeal_service.py`).

Real HTTP layer via `TestClient` + `get_current_user`/`get_db` dependency
overrides, in-memory SQLite -- same established technique as every other
moderation test file in this codebase (`tests/test_moderation_claims.py`,
`tests/test_repeat_offenders.py`, `tests/test_chat_reports.py`).
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
from app.main import app
from app.models.badges import Badge, GamificationProfile, UserBadge
from app.models.chat import ChatMessage, ChatParticipant, ChatRoom
from app.models.chat_moderation import ChatMessageReport, ChatMessageReportEntry
from app.models.enums import AdminRole, ChatReportReason, ModerationAction, StudentVideoStatus
from app.models.moderation_appeal import ModerationAppeal
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import chat_report_service, chat_security, notification_connection_manager, notification_service

# ---------------------------------------------------------------------
# Shared DB fixture -- same raw-SQL-for-JSONB/ARRAY-columns pattern
# established across every prior moderation test file.
# ---------------------------------------------------------------------

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

_CREATE_DANGEROUS_ACTION_CONFIRMATIONS_TABLE_SQL = """
CREATE TABLE dangerous_action_confirmations (
    id VARCHAR(36) PRIMARY KEY,
    action VARCHAR(30) NOT NULL,
    item_type VARCHAR(30) NOT NULL,
    item_id VARCHAR(36) NOT NULL,
    notes TEXT,
    status VARCHAR(20) NOT NULL DEFAULT 'pending',
    is_override BOOLEAN NOT NULL DEFAULT 0,
    requested_by VARCHAR(36),
    requested_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    executed_by VARCHAR(36),
    executed_at DATETIME
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
        await conn.execute(text(_CREATE_DANGEROUS_ACTION_CONFIRMATIONS_TABLE_SQL))
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
                ModerationAppeal.__table__,
            ],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession, *, admin_role: AdminRole | None = None, full_name: str = "Test User"
) -> User:
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name, admin_role=admin_role
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


class _FakeRedis:
    """Same minimal in-memory Redis stand-in as `tests/test_chat_reports.py`'s
    own `_FakeRedis`, plus `delete` -- needed here because `resolve_appeal`'s
    own REVERSIBLE-ban path calls the real `chat_security.unban_user`,
    which issues a real `DEL`."""

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


@pytest.fixture
def fake_redis(monkeypatch):
    redis = _FakeRedis()
    monkeypatch.setattr(chat_security, "_get_redis_client", lambda: redis)
    return redis


@pytest.fixture(autouse=True)
def _fake_notification_broadcast(monkeypatch):
    """This suite's real focus is the DB row `create_notification` writes
    -- not real-time delivery. Without this, every notification-triggering
    call attempts a genuine DNS lookup against this sandbox's unreachable
    `redis://redis:6379`, which is caught by `create_notification`'s own
    best-effort try/except but costs several real seconds PER call across
    dozens of tests here. Patched at the same `_get_redis_client()`
    boundary `notification_connection_manager` itself already isolates.
    """

    class _NoopPublishClient:
        async def publish(self, channel: str, message: str) -> None:
            return None

    monkeypatch.setattr(notification_connection_manager, "_get_redis_client", lambda: _NoopPublishClient())

    # Every notification in this suite is `type="WARNING"` -- the one
    # category `notification_service.HIGHLY_CRITICAL_TYPES` also
    # dispatches a real Celery `.delay()` transactional-email task for.
    # Same real, unreachable-broker cost as the pub/sub client above
    # (`redis://redis:6379` doesn't resolve in this sandbox); patched at
    # the same boundary `notification_service` itself imports.
    class _NoopEmailTask:
        def delay(self, *args, **kwargs) -> None:
            return None

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


async def _notifications_for(db: AsyncSession, user_id: uuid.UUID) -> list[dict]:
    result = await db.execute(
        text("SELECT type, title, message, payload FROM notifications WHERE user_id = :uid"),
        {"uid": str(user_id).replace("-", "") if False else str(user_id)},
    )
    rows = result.all()
    if not rows:
        # SQLite may have persisted the UUID without dashes depending on
        # the bind param's own string form -- fall back to an unfiltered
        # scan + Python-side normalization rather than a brittle query.
        result = await db.execute(text("SELECT user_id, type, title, message, payload FROM notifications"))
        rows = [
            (type_, title, message, payload)
            for uid, type_, title, message, payload in result.all()
            if uid.replace("-", "") == str(user_id).replace("-", "")
        ]
    return [dict(type=t, title=ti, message=m, payload=p) for t, ti, m, p in rows]


# ---------------------------------------------------------------------
# Step 2: real notifications on the video moderation actions
# ---------------------------------------------------------------------


class TestVideoModerationNotifications:
    pytestmark = pytest.mark.asyncio

    async def test_override_reject_notifies_the_student_with_the_real_reason(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session, full_name="Student One")
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "rejected", "notes": "Off-topic content."},
        )
        assert response.status_code == 200

        async with db_session_factory() as session:
            notes = await _notifications_for(session, student.id)
        assert len(notes) == 1
        assert notes[0]["type"] == "WARNING"
        assert "Off-topic content." in notes[0]["message"]
        assert "video_override" in notes[0]["payload"]
        assert '"appealable": true' in notes[0]["payload"]

    async def test_override_reject_with_no_notes_uses_the_honest_fallback_sentence(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(moderator)
        client.post(f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "rejected"})

        async with db_session_factory() as session:
            notes = await _notifications_for(session, student.id)
        assert "No additional reason was provided" in notes[0]["message"]

    async def test_override_approve_still_sends_its_own_pre_existing_achievement_notification(
        self, db_session_factory, client_factory
    ):
        """Confirms the REJECT-branch addition didn't disturb the one
        real notification that already existed before this task."""
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(moderator)
        client.post(f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "approved"})

        async with db_session_factory() as session:
            notes = await _notifications_for(session, student.id)
        assert len(notes) == 1
        assert notes[0]["type"] == "ACHIEVEMENT"

    async def test_reject_and_purge_notifies_the_student(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.APPROVED.value)

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/reject-and-purge-media", json={"notes": "Policy violation."}
        )
        assert response.status_code == 202
        confirmation_id = response.json()["confirmation_id"]

        async with db_session_factory() as session:
            second_moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        approver_client = client_factory(second_moderator)
        approve_response = approver_client.post(
            f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"
        )
        assert approve_response.status_code == 200

        async with db_session_factory() as session:
            notes = await _notifications_for(session, student.id)
        assert len(notes) == 1
        assert "media" in notes[0]["message"].lower() or "removed" in notes[0]["message"].lower()
        assert "video_reject_and_purge" in notes[0]["payload"]

    async def test_delete_permanently_notifies_the_student_including_via_new_optional_body(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(moderator)
        response = client.request(
            "DELETE", f"/api/v1/admin/videos/{video.id}", json={"notes": "Repeated policy violations."}
        )
        assert response.status_code == 202
        confirmation_id = response.json()["confirmation_id"]

        async with db_session_factory() as session:
            second_moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        approver_client = client_factory(second_moderator)
        approve_response = approver_client.post(
            f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"
        )
        assert approve_response.status_code == 200

        async with db_session_factory() as session:
            notes = await _notifications_for(session, student.id)
        assert len(notes) == 1
        assert "Repeated policy violations." in notes[0]["message"]
        assert "video_delete" in notes[0]["payload"]

    async def test_delete_permanently_with_no_body_at_all_still_works_and_uses_fallback(
        self, db_session_factory, client_factory
    ):
        """The real, disclosed gap this task closed: this endpoint took
        NO request body before this task at all -- `payload` must stay
        genuinely optional."""
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(moderator)
        response = client.delete(f"/api/v1/admin/videos/{video.id}")
        assert response.status_code == 202
        confirmation_id = response.json()["confirmation_id"]

        async with db_session_factory() as session:
            second_moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        approver_client = client_factory(second_moderator)
        approve_response = approver_client.post(
            f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"
        )
        assert approve_response.status_code == 200

        async with db_session_factory() as session:
            notes = await _notifications_for(session, student.id)
        assert len(notes) == 1
        assert "No additional reason was provided" in notes[0]["message"]


# ---------------------------------------------------------------------
# Step 2: real notifications on the chat-report moderation actions
# ---------------------------------------------------------------------


async def _make_room_and_message(db: AsyncSession, *, sender_id: uuid.UUID, other_id: uuid.UUID) -> ChatMessage:
    room = ChatRoom(id=uuid.uuid4(), is_group=False)
    db.add(room)
    await db.flush()
    db.add(ChatParticipant(room_id=room.id, user_id=sender_id))
    db.add(ChatParticipant(room_id=room.id, user_id=other_id))
    await db.commit()
    message = ChatMessage(id=uuid.uuid4(), room_id=room.id, sender_id=sender_id, content="spam content")
    db.add(message)
    await db.commit()
    await db.refresh(message)
    return message


class TestChatReportNotifications:
    pytestmark = pytest.mark.asyncio

    async def test_delete_message_notifies_the_sender_with_real_reason_category(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            sender = await _make_user(session, full_name="Sender")
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            message = await _make_room_and_message(session, sender_id=sender.id, other_id=reporter.id)
            report, _entry, _total = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.HARASSMENT, details=None
            )
            await session.commit()
            report_id = report.id

        client = client_factory(moderator)
        response = client.request(
            "DELETE", f"/api/v1/admin/chat/reports/{report_id}/message", json={"notes": "Confirmed harassment."}
        )
        assert response.status_code == 202
        confirmation_id = response.json()["confirmation_id"]

        async with db_session_factory() as session:
            second_moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        approver_client = client_factory(second_moderator)
        approve_response = approver_client.post(
            f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"
        )
        assert approve_response.status_code == 200

        async with db_session_factory() as session:
            notes = await _notifications_for(session, sender.id)
        assert len(notes) == 1
        assert "harassment" in notes[0]["message"]
        assert "Confirmed harassment." in notes[0]["message"]
        assert "chat_message_delete" in notes[0]["payload"]

    async def test_delete_message_with_no_body_still_works(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            message = await _make_room_and_message(session, sender_id=sender.id, other_id=reporter.id)
            report, _entry, _total = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()
            report_id = report.id

        client = client_factory(moderator)
        response = client.delete(f"/api/v1/admin/chat/reports/{report_id}/message")
        assert response.status_code == 202
        confirmation_id = response.json()["confirmation_id"]

        async with db_session_factory() as session:
            second_moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        approver_client = client_factory(second_moderator)
        approve_response = approver_client.post(
            f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve"
        )
        assert approve_response.status_code == 200

        async with db_session_factory() as session:
            notes = await _notifications_for(session, sender.id)
        assert len(notes) == 1
        assert "spam" in notes[0]["message"]

    async def test_escalate_ban_notifies_the_banned_sender(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            message = await _make_room_and_message(session, sender_id=sender.id, other_id=reporter.id)
            report, _entry, _total = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()
            report_id = report.id

        client = client_factory(moderator)
        response = client.post(
            f"/api/v1/admin/chat/reports/{report_id}/escalate-ban",
            json={"notes": "Repeated spam.", "duration_seconds": 1800},
        )
        assert response.status_code == 200

        async with db_session_factory() as session:
            notes = await _notifications_for(session, sender.id)
        assert len(notes) == 1
        assert "30 minute" in notes[0]["message"]
        assert "spam" in notes[0]["message"]
        assert "chat_report_escalate_ban" in notes[0]["payload"]


# ---------------------------------------------------------------------
# Step 3: appeal submission
# ---------------------------------------------------------------------


class TestAppealSubmission:
    pytestmark = pytest.mark.asyncio

    async def test_real_affected_student_can_submit_an_appeal_against_a_reversible_action(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        mod_client = client_factory(moderator)
        override_response = mod_client.post(
            f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "rejected", "notes": "Off-topic."}
        )
        assert override_response.status_code == 200

        async with db_session_factory() as session:
            row = (
                await session.execute(text("SELECT id FROM moderation_log_entries WHERE action='video_override'"))
            ).one()
            log_entry_id = row[0]

        student_client = client_factory(student)
        response = student_client.post(
            "/api/v1/moderation/appeals",
            json={"log_entry_id": log_entry_id, "message": "I was quoting a source, not being off-topic."},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "open"
        assert body["can_be_restored"] is True
        assert body["action"] == "video_override"

    async def test_a_different_user_cannot_appeal_someone_elses_action(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
            impostor = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client_factory(moderator).post(
            f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "rejected"}
        )

        async with db_session_factory() as session:
            row = (
                await session.execute(text("SELECT id FROM moderation_log_entries WHERE action='video_override'"))
            ).one()
            log_entry_id = row[0]

        response = client_factory(impostor).post(
            "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "This wasn't me."}
        )
        assert response.status_code == 403

    async def test_a_non_appealable_action_is_rejected_with_400(self, db_session_factory, client_factory):
        """`CHAT_UNBAN`/`CHAT_REPORT_DISMISS` (and this task's own
        `APPEAL_UPHELD`/`APPEAL_DENIED`) are neutral-or-positive -- no
        real grievance to appeal."""
        async with db_session_factory() as session:
            user = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            entry_id = uuid.uuid4()
            await session.execute(
                text(
                    "INSERT INTO moderation_log_entries (id, actor_id, actor_role, action, target_type, "
                    "target_id, outcome) VALUES (:id, :actor_id, :actor_role, 'chat_unban', 'user', :target_id, '{}')"
                ),
                {"id": entry_id.hex, "actor_id": moderator.id.hex, "actor_role": "moderator", "target_id": user.id.hex},
            )
            await session.commit()

        response = client_factory(user).post(
            "/api/v1/moderation/appeals", json={"log_entry_id": str(entry_id), "message": "I want to appeal this."}
        )
        assert response.status_code == 400

    async def test_a_second_open_appeal_on_the_same_entry_is_rejected_with_409(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client_factory(moderator).post(f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "rejected"})

        async with db_session_factory() as session:
            row = (
                await session.execute(text("SELECT id FROM moderation_log_entries WHERE action='video_override'"))
            ).one()
            log_entry_id = row[0]

        student_client = client_factory(student)
        first = student_client.post(
            "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "First appeal."}
        )
        assert first.status_code == 201
        second = student_client.post(
            "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "Second appeal."}
        )
        assert second.status_code == 409

    async def test_acknowledgment_only_action_is_still_appealable_but_flagged_not_restorable(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        request_resp = client_factory(moderator).delete(f"/api/v1/admin/videos/{video.id}")
        confirmation_id = request_resp.json()["confirmation_id"]
        async with db_session_factory() as session:
            second_moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client_factory(second_moderator).post(f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve")

        async with db_session_factory() as session:
            row = (
                await session.execute(text("SELECT id FROM moderation_log_entries WHERE action='video_delete'"))
            ).one()
            log_entry_id = row[0]

        response = client_factory(student).post(
            "/api/v1/moderation/appeals",
            json={"log_entry_id": log_entry_id, "message": "My video shouldn't have been deleted."},
        )
        assert response.status_code == 201
        assert response.json()["can_be_restored"] is False


# ---------------------------------------------------------------------
# Step 3: appeal resolution -- tier gating
# ---------------------------------------------------------------------


async def _seed_rejected_override_appeal(db_session_factory, client_factory) -> tuple[uuid.UUID, uuid.UUID]:
    """Returns (student_id, appeal_id) for a fresh, real, open appeal
    against a real VIDEO_OVERRIDE-into-rejected entry."""
    async with db_session_factory() as session:
        student = await _make_user(session)
        moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

    client_factory(moderator).post(f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "rejected"})

    async with db_session_factory() as session:
        row = (
            await session.execute(text("SELECT id FROM moderation_log_entries WHERE action='video_override'"))
        ).one()
        log_entry_id = row[0]

    appeal_response = client_factory(student).post(
        "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "Please review."}
    )
    return student.id, uuid.UUID(appeal_response.json()["id"])


class TestAppealResolutionTierGating:
    pytestmark = pytest.mark.asyncio

    async def test_plain_moderator_gets_403_on_the_appeals_queue(self, db_session_factory, client_factory):
        student_id, appeal_id = await _seed_rejected_override_appeal(db_session_factory, client_factory)
        async with db_session_factory() as session:
            plain_moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)

        response = client_factory(plain_moderator).get("/api/v1/admin/appeals")
        assert response.status_code == 403

    async def test_plain_moderator_gets_403_on_resolve(self, db_session_factory, client_factory):
        student_id, appeal_id = await _seed_rejected_override_appeal(db_session_factory, client_factory)
        async with db_session_factory() as session:
            plain_moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)

        response = client_factory(plain_moderator).post(
            f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "uphold"}
        )
        assert response.status_code == 403

    async def test_senior_moderator_can_view_and_resolve(self, db_session_factory, client_factory):
        student_id, appeal_id = await _seed_rejected_override_appeal(db_session_factory, client_factory)
        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)

        client = client_factory(senior_mod)
        queue = client.get("/api/v1/admin/appeals")
        assert queue.status_code == 200
        assert any(str(appeal_id) == item["id"] for item in queue.json()["items"])

        resolve = client.post(f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "uphold"})
        assert resolve.status_code == 200

    async def test_owner_can_also_resolve(self, db_session_factory, client_factory):
        student_id, appeal_id = await _seed_rejected_override_appeal(db_session_factory, client_factory)
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)

        response = client_factory(owner).post(
            f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "deny"}
        )
        assert response.status_code == 200

    async def test_resolving_an_already_resolved_appeal_returns_409(self, db_session_factory, client_factory):
        student_id, appeal_id = await _seed_rejected_override_appeal(db_session_factory, client_factory)
        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)

        client = client_factory(senior_mod)
        first = client.post(f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "deny"})
        assert first.status_code == 200
        second = client.post(f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "deny"})
        assert second.status_code == 409


# ---------------------------------------------------------------------
# Step 3.3: the core honest reversal-per-action-type logic
# ---------------------------------------------------------------------


class TestReversalSemantics:
    pytestmark = pytest.mark.asyncio

    async def test_upholding_a_rejected_video_override_appeal_really_restores_approved_status(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client_factory(moderator).post(
            f"/api/v1/admin/videos/{video.id}/override", json={"target_status": "rejected"}
        )

        async with db_session_factory() as session:
            row = (
                await session.execute(text("SELECT id FROM moderation_log_entries WHERE action='video_override'"))
            ).one()
            log_entry_id = row[0]

        appeal_response = client_factory(student).post(
            "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "Please review."}
        )
        appeal_id = appeal_response.json()["id"]

        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)

        resolve = client_factory(senior_mod).post(
            f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "uphold"}
        )
        assert resolve.status_code == 200
        body = resolve.json()
        assert body["reversal_applied"] is True
        assert "APPROVED" in body["reversal_detail"]

        async with db_session_factory() as session:
            refreshed = await session.get(StudentVideo, video.id)
            assert refreshed.status == StudentVideoStatus.APPROVED.value

    async def test_upholding_an_escalate_ban_appeal_really_lifts_the_ban(
        self, db_session_factory, client_factory, fake_redis
    ):
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            message = await _make_room_and_message(session, sender_id=sender.id, other_id=reporter.id)
            report, _entry, _total = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()
            report_id = report.id

        client_factory(moderator).post(
            f"/api/v1/admin/chat/reports/{report_id}/escalate-ban",
            json={"notes": "spam", "duration_seconds": 1800},
        )
        assert await chat_security.is_user_banned(sender.id) is True

        async with db_session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT id FROM moderation_log_entries WHERE action='chat_report_escalate_ban'")
                )
            ).one()
            log_entry_id = row[0]

        appeal_response = client_factory(sender).post(
            "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "It wasn't spam."}
        )
        appeal_id = appeal_response.json()["id"]
        assert appeal_response.json()["can_be_restored"] is True

        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)

        resolve = client_factory(senior_mod).post(
            f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "uphold"}
        )
        assert resolve.status_code == 200
        assert resolve.json()["reversal_applied"] is True
        assert await chat_security.is_user_banned(sender.id) is False

    async def test_upholding_a_permanently_deleted_video_appeal_cannot_restore_it(
        self, db_session_factory, client_factory
    ):
        """The core honest requirement: upholding an appeal against an
        ACKNOWLEDGMENT-ONLY action must NEVER report `reversal_applied=True`."""
        async with db_session_factory() as session:
            student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        request_resp = client_factory(moderator).delete(f"/api/v1/admin/videos/{video.id}")
        confirmation_id = request_resp.json()["confirmation_id"]
        async with db_session_factory() as session:
            second_moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client_factory(second_moderator).post(f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve")

        async with db_session_factory() as session:
            row = (
                await session.execute(text("SELECT id FROM moderation_log_entries WHERE action='video_delete'"))
            ).one()
            log_entry_id = row[0]

        appeal_response = client_factory(student).post(
            "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "It should not have been deleted."}
        )
        appeal_id = appeal_response.json()["id"]
        assert appeal_response.json()["can_be_restored"] is False

        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)

        resolve = client_factory(senior_mod).post(
            f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "uphold"}
        )
        assert resolve.status_code == 200
        body = resolve.json()
        assert body["status"] == "upheld"
        assert body["reversal_applied"] is False
        assert "cannot be restored" in body["reversal_detail"]

    async def test_denying_an_appeal_performs_no_reversal_and_sends_no_false_hope(
        self, db_session_factory, client_factory
    ):
        student_id, appeal_id = await _seed_rejected_override_appeal(db_session_factory, client_factory)
        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)

        resolve = client_factory(senior_mod).post(
            f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "deny"}
        )
        assert resolve.status_code == 200
        body = resolve.json()
        assert body["status"] == "denied"
        assert body["reversal_applied"] is False

    async def test_upholding_a_ban_appeal_whose_ban_already_expired_reports_honestly(
        self, db_session_factory, client_factory, fake_redis
    ):
        """The Redis TTL naturally expiring before the appeal is
        resolved must not be reported as "we unbanned you" -- there was
        nothing left to lift."""
        async with db_session_factory() as session:
            sender = await _make_user(session)
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            message = await _make_room_and_message(session, sender_id=sender.id, other_id=reporter.id)
            report, _entry, _total = await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()
            report_id = report.id

        client_factory(moderator).post(
            f"/api/v1/admin/chat/reports/{report_id}/escalate-ban",
            json={"notes": "spam", "duration_seconds": 1800},
        )

        async with db_session_factory() as session:
            row = (
                await session.execute(
                    text("SELECT id FROM moderation_log_entries WHERE action='chat_report_escalate_ban'")
                )
            ).one()
            log_entry_id = row[0]

        appeal_response = client_factory(sender).post(
            "/api/v1/moderation/appeals", json={"log_entry_id": log_entry_id, "message": "It wasn't spam."}
        )
        appeal_id = appeal_response.json()["id"]

        # Simulate the ban having already expired naturally (TTL lapsed)
        # before a Senior Moderator gets to it.
        await chat_security.unban_user(sender.id)

        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)

        resolve = client_factory(senior_mod).post(
            f"/api/v1/admin/appeals/{appeal_id}/resolve", json={"decision": "uphold"}
        )
        assert resolve.status_code == 200
        body = resolve.json()
        assert body["reversal_applied"] is False
        assert "already expired" in body["reversal_detail"]
