"""Behavioral tests for the High-Risk-Content View:
`app/services/high_risk_service.py`'s real composition of four existing
signals, and the new Owner-only `GET /admin/high-risk` endpoint
(`app/api/v1/endpoints/admin_high_risk.py`). See both modules' own
docstrings for the full Step 1 investigation (no new risk-detection
logic -- this feature reuses `repeat_offender_service`,
`chat_security.list_banned_users`, the open chat-report queue, and the
pending video-review queue exactly as they already exist).

Real HTTP layer via `TestClient` + `get_current_user`/`get_db` dependency
overrides, in-memory SQLite -- same established technique as
`tests/test_repeat_offenders.py`, whose fixture DDL this file mirrors
directly (same tables needed: users/student_videos/moderation_log_entries
plus the chat rooms/participants/messages/reports tables via
`Base.metadata.create_all`).
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
from app.models.enums import AdminRole, ChatReportReason, ModerationAction, ModerationTargetType
from app.models.moderation_log import ModerationLogEntry
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import chat_report_service, chat_security, repeat_offender_service

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
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER,
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
    db: AsyncSession, *, admin_role: AdminRole | None = None, full_name: str = "Test User"
) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name, admin_role=admin_role)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str = "rejected", created_at=None) -> StudentVideo:
    video = StudentVideo(
        id=uuid.uuid4(), student_id=student_id, title="Test Video", status=status,
        created_at=created_at or _now(),
    )
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


def _now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


async def _log(
    db: AsyncSession,
    *,
    action: ModerationAction,
    target_type: ModerationTargetType,
    target_id: uuid.UUID,
    outcome: dict,
    actor_id: uuid.UUID | None = None,
    actor_role: AdminRole | None = None,
) -> ModerationLogEntry:
    entry = ModerationLogEntry(
        id=uuid.uuid4(), actor_id=actor_id, actor_role=actor_role, action=action,
        target_type=target_type, target_id=target_id, outcome=outcome,
    )
    db.add(entry)
    await db.commit()
    return entry


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


async def _make_open_report(
    db: AsyncSession, *, sender_id: uuid.UUID, reporter_id: uuid.UUID, room_id: uuid.UUID | None = None
) -> ChatMessageReport:
    room = room_id
    if room is None:
        room_obj = await _make_room_with_participants(db, sender_id, reporter_id)
        room = room_obj.id
    message = await _make_message(db, room_id=room, sender_id=sender_id)
    case, _entry, _created = await chat_report_service.create_report(
        db, message=message, reporter_id=reporter_id, reason=ChatReportReason.HARASSMENT, details=None
    )
    await db.commit()
    return case


class _FakeRedis:
    """Same shape as `tests/test_admin_roles.py`'s own `_FakeRedis` --
    the one with `scan_iter`/`delete`, since `chat_security.list_banned_users`
    (the real function this task wires into its first admin caller) needs
    both, unlike the narrower fakes some other test files use."""

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


pytestmark = pytest.mark.asyncio


class TestHighRiskAccess:
    async def test_plain_student_gets_403(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            student = await _make_user(session)
        client = client_factory(student)
        response = client.get("/api/v1/admin/high-risk")
        assert response.status_code == 403

    async def test_moderator_gets_403(self, db_session_factory, client_factory, fake_redis):
        """Owner-only, deliberately stricter than the Moderator-tier
        per-case views -- see `admin_high_risk.py`'s own docstring."""
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/high-risk")
        assert response.status_code == 403

    async def test_senior_moderator_gets_403(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        response = client.get("/api/v1/admin/high-risk")
        assert response.status_code == 403

    async def test_owner_gets_200(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/high-risk")
        assert response.status_code == 200


class TestHighRiskEmptyState:
    async def test_no_signals_returns_real_honest_empty_state(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()

        assert body["flagged_users"] == []
        assert body["banned_users"] == []
        assert body["open_chat_reports"] == {"count": 0, "oldest_item_at": None, "reason": "No open chat reports"}
        assert body["pending_video_reviews"] == {"count": 0, "oldest_item_at": None, "reason": "No videos awaiting review"}
        assert body["window_days"] == repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS
        assert body["threshold"] == repeat_offender_service.REPEAT_OFFENDER_THRESHOLD
        assert body["generated_at"] is not None


class TestHighRiskFlaggedUsers:
    async def test_user_at_threshold_is_surfaced_with_a_real_explainable_reason(
        self, db_session_factory, client_factory, fake_redis
    ):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            offender = await _make_user(session, full_name="Repeat Offender")
            for _ in range(repeat_offender_service.REPEAT_OFFENDER_THRESHOLD):
                await _log(
                    session, action=ModerationAction.CHAT_AUTO_BAN, target_type=ModerationTargetType.USER,
                    target_id=offender.id, outcome={"trigger": "arabic_filter"},
                )

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()

        assert len(body["flagged_users"]) == 1
        item = body["flagged_users"][0]
        assert item["user_id"] == str(offender.id)
        assert item["full_name"] == "Repeat Offender"
        assert item["recent_action_count"] == repeat_offender_service.REPEAT_OFFENDER_THRESHOLD
        assert item["reason"] == (
            f"{repeat_offender_service.REPEAT_OFFENDER_THRESHOLD} moderation actions in the last "
            f"{repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS} days"
        )

    async def test_user_below_threshold_is_not_surfaced(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            not_quite = await _make_user(session)
            for _ in range(repeat_offender_service.REPEAT_OFFENDER_THRESHOLD - 1):
                await _log(
                    session, action=ModerationAction.CHAT_AUTO_BAN, target_type=ModerationTargetType.USER,
                    target_id=not_quite.id, outcome={"trigger": "arabic_filter"},
                )

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()
        assert body["flagged_users"] == []

    async def test_matches_get_flagged_users_exactly_same_underlying_data(
        self, db_session_factory, client_factory, fake_redis
    ):
        """This view must never disagree with `GET /admin/flagged-users`
        about the same real facts -- both call the exact same
        `repeat_offender_service` functions."""
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            offender = await _make_user(session)
            for _ in range(4):
                await _log(
                    session, action=ModerationAction.CHAT_AUTO_BAN, target_type=ModerationTargetType.USER,
                    target_id=offender.id, outcome={"trigger": "arabic_filter"},
                )

        client = client_factory(owner)
        high_risk_body = client.get("/api/v1/admin/high-risk").json()
        flagged_body = client.get("/api/v1/admin/flagged-users").json()

        assert len(high_risk_body["flagged_users"]) == 1
        assert high_risk_body["flagged_users"][0]["recent_action_count"] == flagged_body["items"][0]["recent_action_count"]
        assert high_risk_body["flagged_users"][0]["user_id"] == flagged_body["items"][0]["user_id"]


class TestHighRiskBannedUsers:
    async def test_currently_banned_user_is_surfaced_with_real_remaining_ttl(
        self, db_session_factory, client_factory, fake_redis
    ):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            banned = await _make_user(session, full_name="Banned Student")

        await chat_security.ban_user(banned.id, duration_seconds=3600)

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()

        assert len(body["banned_users"]) == 1
        item = body["banned_users"][0]
        assert item["user_id"] == str(banned.id)
        assert item["full_name"] == "Banned Student"
        assert item["remaining_seconds"] == 3600
        assert item["reason"] == "Currently serving a chat ban"

    async def test_not_currently_banned_user_is_absent(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            await _make_user(session)
        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()
        assert body["banned_users"] == []

    async def test_expired_ban_is_absent(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            student = await _make_user(session)

        await chat_security.ban_user(student.id, duration_seconds=60)
        await fake_redis.delete(f"chat:ban:{student.id}")  # simulate real TTL lapse

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()
        assert body["banned_users"] == []


class TestHighRiskQueueBacklogs:
    async def test_open_chat_reports_counted_with_real_oldest_timestamp(
        self, db_session_factory, client_factory, fake_redis
    ):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            sender = await _make_user(session)
            reporter1 = await _make_user(session)
            reporter2 = await _make_user(session)
            case1 = await _make_open_report(session, sender_id=sender.id, reporter_id=reporter1.id)
            # A second, distinct message/report so both are real, separate open cases.
            room2 = await _make_room_with_participants(session, sender.id, reporter2.id)
            await _make_open_report(session, sender_id=sender.id, reporter_id=reporter2.id, room_id=room2.id)

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()

        assert body["open_chat_reports"]["count"] == 2
        assert body["open_chat_reports"]["reason"] == "2 chat report(s) awaiting review"
        assert body["open_chat_reports"]["oldest_item_at"] is not None

    async def test_dismissed_reports_excluded_from_open_backlog(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            sender = await _make_user(session)
            reporter = await _make_user(session)
            case = await _make_open_report(session, sender_id=sender.id, reporter_id=reporter.id)
            case.status = "dismissed"
            session.add(case)
            await session.commit()

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()
        assert body["open_chat_reports"]["count"] == 0

    async def test_pending_video_reviews_counted_excluding_approved(
        self, db_session_factory, client_factory, fake_redis
    ):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            student = await _make_user(session)
            await _make_video(session, student_id=student.id, status="rejected")
            await _make_video(session, student_id=student.id, status="failed")
            await _make_video(session, student_id=student.id, status="pending_ai")
            await _make_video(session, student_id=student.id, status="approved")  # must be excluded

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()

        assert body["pending_video_reviews"]["count"] == 3
        assert body["pending_video_reviews"]["reason"] == "3 video(s) awaiting review"
        assert body["pending_video_reviews"]["oldest_item_at"] is not None

    async def test_oldest_pending_video_is_the_real_earliest_one(self, db_session_factory, client_factory, fake_redis):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            student = await _make_user(session)
            older = await _make_video(
                session, student_id=student.id, status="rejected",
                created_at=_now() - dt.timedelta(days=5),
            )
            await _make_video(session, student_id=student.id, status="failed", created_at=_now())

        client = client_factory(owner)
        body = client.get("/api/v1/admin/high-risk").json()

        oldest_at = dt.datetime.fromisoformat(body["pending_video_reviews"]["oldest_item_at"].replace("Z", "+00:00"))
        assert abs((oldest_at - older.created_at).total_seconds()) < 2
