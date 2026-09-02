"""Behavioral tests for Repeat-Offender Detection (Idea #17):
`app/services/repeat_offender_service.py`'s real per-action-type offender
resolution + lookback-window/threshold logic, the new `CHAT_AUTO_BAN`
durable-logging addition (`actor_id`/`actor_role` now `None` for a
system-initiated entry), the inline `offender_recent_action_count` field
wired into both the video review queue and the chat-reports queue, and
the new Owner-only `GET /admin/flagged-users` endpoint.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db` dependency
overrides, in-memory SQLite -- same established technique as
`tests/test_moderation_claims.py`/`tests/test_chat_reports.py`.
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
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType
from app.models.moderation_log import ModerationLogEntry
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import repeat_offender_service

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

# Repeat-Offender Detection addition (migration 0041): `actor_role` is no
# longer `NOT NULL` -- the whole point of this suite's `CHAT_AUTO_BAN`
# coverage is a real row with `actor_id IS NULL AND actor_role IS NULL`,
# so this fixture DDL must actually allow that (every prior test file's
# copy of this table still has `actor_role VARCHAR(20) NOT NULL`,
# deliberately NOT touched here -- they never write a `None` actor_role,
# so widening them would be an unrelated, undisclosed change).
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
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name, admin_role=admin_role
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str = "rejected") -> StudentVideo:
    video = StudentVideo(id=uuid.uuid4(), student_id=student_id, title="Test Video", status=status)
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
    created_at: dt.datetime | None = None,
) -> ModerationLogEntry:
    """Direct-insert helper, deliberately bypassing
    `moderation_log_service.log_action` -- that function never sets
    `created_at` itself (relies on `server_default`), so seeding known
    PAST timestamps for the lookback-window boundary tests requires
    constructing the row directly, same posture this codebase already
    uses in `tests/test_moderation_stats.py` for multi-day seeded data.
    """
    entry = ModerationLogEntry(
        id=uuid.uuid4(),
        actor_id=actor_id,
        actor_role=actor_role,
        action=action,
        target_type=target_type,
        target_id=target_id,
        outcome=outcome,
        created_at=created_at or _now(),
    )
    db.add(entry)
    await db.commit()
    return entry


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
# Service-layer: per-action-type offender resolution
# ---------------------------------------------------------------------


class TestOffenderResolution:
    pytestmark = pytest.mark.asyncio

    async def test_chat_auto_ban_direct_target_counts(self, db_session_factory):
        """The real gap this task closes: a `CHAT_AUTO_BAN` entry has
        `actor_id=None`/`actor_role=None` (system-initiated) but a real,
        directly-readable `target_id` -- no `outcome` extraction needed."""
        async with db_session_factory() as session:
            offender = await _make_user(session)
            await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=offender.id,
                outcome={"trigger": "arabic_filter"},
                actor_id=None,
                actor_role=None,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session)
            assert counts.get(offender.id) == 1

    async def test_video_delete_reads_outcome_student_id(self, db_session_factory):
        """`VIDEO_DELETE`'s target row is gone by write time -- the real
        offender was captured into `outcome['student_id']`."""
        async with db_session_factory() as session:
            offender = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            await _log(
                session,
                action=ModerationAction.VIDEO_DELETE,
                target_type=ModerationTargetType.STUDENT_VIDEO,
                target_id=uuid.uuid4(),
                outcome={"video_deleted": True, "student_id": str(offender.id)},
                actor_id=moderator.id,
                actor_role=AdminRole.SENIOR_MODERATOR,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session)
            assert counts.get(offender.id) == 1

    async def test_video_reject_and_purge_joins_surviving_row(self, db_session_factory):
        """`VIDEO_REJECT_AND_PURGE` keeps the `StudentVideo` row -- the
        real offender is recovered via a live join on `target_id`."""
        async with db_session_factory() as session:
            offender = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            video = await _make_video(session, student_id=offender.id, status="rejected")
            await _log(
                session,
                action=ModerationAction.VIDEO_REJECT_AND_PURGE,
                target_type=ModerationTargetType.STUDENT_VIDEO,
                target_id=video.id,
                outcome={"new_status": "rejected", "r2_urls_purged": []},
                actor_id=moderator.id,
                actor_role=AdminRole.SENIOR_MODERATOR,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session)
            assert counts.get(offender.id) == 1

    async def test_video_override_rejected_counts_but_approved_does_not(self, db_session_factory):
        """A REJECTING override is punitive; an override that APPROVES a
        video must never count against its own owner."""
        async with db_session_factory() as session:
            offender = await _make_user(session)
            clean_user = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            rejected_video = await _make_video(session, student_id=offender.id, status="rejected")
            approved_video = await _make_video(session, student_id=clean_user.id, status="approved")

            await _log(
                session,
                action=ModerationAction.VIDEO_OVERRIDE,
                target_type=ModerationTargetType.STUDENT_VIDEO,
                target_id=rejected_video.id,
                outcome={"new_status": "rejected", "notes": None},
                actor_id=moderator.id,
                actor_role=AdminRole.MODERATOR,
            )
            await _log(
                session,
                action=ModerationAction.VIDEO_OVERRIDE,
                target_type=ModerationTargetType.STUDENT_VIDEO,
                target_id=approved_video.id,
                outcome={"new_status": "approved", "notes": None},
                actor_id=moderator.id,
                actor_role=AdminRole.MODERATOR,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session)
            assert counts.get(offender.id) == 1
            assert clean_user.id not in counts

    async def test_chat_message_delete_reads_outcome_sender_id(self, db_session_factory):
        async with db_session_factory() as session:
            offender = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
            await _log(
                session,
                action=ModerationAction.CHAT_MESSAGE_DELETE,
                target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
                target_id=uuid.uuid4(),
                outcome={"message_id": str(uuid.uuid4()), "sender_id": str(offender.id)},
                actor_id=moderator.id,
                actor_role=AdminRole.SENIOR_MODERATOR,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session)
            assert counts.get(offender.id) == 1

    async def test_chat_report_escalate_ban_reads_outcome_banned_user_id(self, db_session_factory):
        async with db_session_factory() as session:
            offender = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            await _log(
                session,
                action=ModerationAction.CHAT_REPORT_ESCALATE_BAN,
                target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
                target_id=uuid.uuid4(),
                outcome={"banned_user_id": str(offender.id), "duration_seconds": 1800, "notes": None},
                actor_id=moderator.id,
                actor_role=AdminRole.MODERATOR,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session)
            assert counts.get(offender.id) == 1

    async def test_chat_unban_and_dismiss_are_never_counted(self, db_session_factory):
        """Both are non-punitive by this module's own design -- an unban
        is mitigating, a dismissal means a human already concluded the
        report was baseless."""
        async with db_session_factory() as session:
            user = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            await _log(
                session,
                action=ModerationAction.CHAT_UNBAN,
                target_type=ModerationTargetType.USER,
                target_id=user.id,
                outcome={"was_banned": True},
                actor_id=moderator.id,
                actor_role=AdminRole.MODERATOR,
            )
            await _log(
                session,
                action=ModerationAction.CHAT_REPORT_DISMISS,
                target_type=ModerationTargetType.CHAT_MESSAGE_REPORT,
                target_id=uuid.uuid4(),
                outcome={"notes": "baseless"},
                actor_id=moderator.id,
                actor_role=AdminRole.MODERATOR,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session)
            assert user.id not in counts


# ---------------------------------------------------------------------
# Service-layer: lookback window boundary + threshold
# ---------------------------------------------------------------------


class TestLookbackWindowAndThreshold:
    pytestmark = pytest.mark.asyncio

    async def test_entry_just_inside_window_counts(self, db_session_factory):
        async with db_session_factory() as session:
            offender = await _make_user(session)
            just_inside = _now() - dt.timedelta(days=29, hours=23)
            await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=offender.id,
                outcome={},
                created_at=just_inside,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session, window_days=30)
            assert counts.get(offender.id) == 1

    async def test_entry_just_outside_window_is_excluded(self, db_session_factory):
        async with db_session_factory() as session:
            offender = await _make_user(session)
            just_outside = _now() - dt.timedelta(days=31)
            await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=offender.id,
                outcome={},
                created_at=just_outside,
            )

            counts = await repeat_offender_service.get_recent_offense_counts(session, window_days=30)
            assert offender.id not in counts

    async def test_flagged_users_only_includes_at_or_above_threshold(self, db_session_factory):
        async with db_session_factory() as session:
            heavy_offender = await _make_user(session, full_name="Heavy Offender")
            light_offender = await _make_user(session, full_name="Light Offender")

            for _ in range(3):
                await _log(
                    session,
                    action=ModerationAction.CHAT_AUTO_BAN,
                    target_type=ModerationTargetType.USER,
                    target_id=heavy_offender.id,
                    outcome={},
                )
            await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=light_offender.id,
                outcome={},
            )

            flagged = await repeat_offender_service.get_flagged_users(session, threshold=3)
            flagged_ids = {user_id for user_id, _count in flagged}
            assert heavy_offender.id in flagged_ids
            assert light_offender.id not in flagged_ids

    async def test_flagged_users_sorted_highest_first(self, db_session_factory):
        async with db_session_factory() as session:
            top = await _make_user(session)
            second = await _make_user(session)

            for _ in range(5):
                await _log(
                    session,
                    action=ModerationAction.CHAT_AUTO_BAN,
                    target_type=ModerationTargetType.USER,
                    target_id=top.id,
                    outcome={},
                )
            for _ in range(3):
                await _log(
                    session,
                    action=ModerationAction.CHAT_AUTO_BAN,
                    target_type=ModerationTargetType.USER,
                    target_id=second.id,
                    outcome={},
                )

            flagged = await repeat_offender_service.get_flagged_users(session, threshold=3)
            assert flagged[0][0] == top.id
            assert flagged[0][1] == 5
            assert flagged[1][0] == second.id
            assert flagged[1][1] == 3


# ---------------------------------------------------------------------
# Model/migration: actor_id/actor_role both nullable (system-initiated)
# ---------------------------------------------------------------------


class TestSystemInitiatedLogEntry:
    pytestmark = pytest.mark.asyncio

    async def test_log_entry_with_no_human_actor_persists_and_reads_back(self, db_session_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
            entry = await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=user.id,
                outcome={"duration_seconds": 1800, "trigger": "arabic_filter"},
                actor_id=None,
                actor_role=None,
            )

            refreshed = await session.get(ModerationLogEntry, entry.id)
            assert refreshed is not None
            assert refreshed.actor_id is None
            assert refreshed.actor_role is None
            assert refreshed.action == ModerationAction.CHAT_AUTO_BAN


# ---------------------------------------------------------------------
# Real HTTP: inline offender_recent_action_count on the two queues
# ---------------------------------------------------------------------


class TestInlineOffenderCountOnQueues:
    pytestmark = pytest.mark.asyncio

    async def test_video_queue_item_shows_real_recent_action_count(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            offender = await _make_user(session, full_name="Flagged Student")
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=offender.id, status="rejected")
            await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=offender.id,
                outcome={},
            )
            await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=offender.id,
                outcome={},
            )

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 200
        item = next(i for i in response.json()["items"] if i["id"] == str(video.id))
        assert item["offender_recent_action_count"] == 2

    async def test_video_queue_item_defaults_to_zero_for_clean_owner(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            clean_student = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            video = await _make_video(session, student_id=clean_student.id, status="rejected")

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/videos/review-queue")
        assert response.status_code == 200
        item = next(i for i in response.json()["items"] if i["id"] == str(video.id))
        assert item["offender_recent_action_count"] == 0

    async def test_chat_reports_queue_item_shows_real_recent_action_count(self, db_session_factory, client_factory):
        from app.models.enums import ChatReportReason
        from app.services import chat_report_service

        async with db_session_factory() as session:
            sender = await _make_user(session, full_name="Flagged Sender")
            reporter = await _make_user(session)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            room = ChatRoom(id=uuid.uuid4(), is_group=False)
            session.add(room)
            await session.flush()
            session.add(ChatParticipant(room_id=room.id, user_id=sender.id))
            session.add(ChatParticipant(room_id=room.id, user_id=reporter.id))
            await session.commit()
            message = ChatMessage(id=uuid.uuid4(), room_id=room.id, sender_id=sender.id, content="spam")
            session.add(message)
            await session.commit()
            await session.refresh(message)

            await chat_report_service.create_report(
                session, message=message, reporter_id=reporter.id, reason=ChatReportReason.SPAM, details=None
            )
            await session.commit()

            await _log(
                session,
                action=ModerationAction.CHAT_AUTO_BAN,
                target_type=ModerationTargetType.USER,
                target_id=sender.id,
                outcome={},
            )

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/chat/reports")
        assert response.status_code == 200
        item = next(i for i in response.json()["items"] if i["message"]["id"] == str(message.id))
        assert item["offender_recent_action_count"] == 1


# ---------------------------------------------------------------------
# Real HTTP: GET /admin/flagged-users -- Owner-only
# ---------------------------------------------------------------------


class TestFlaggedUsersEndpoint:
    pytestmark = pytest.mark.asyncio

    async def test_owner_sees_real_flagged_users(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            offender = await _make_user(session, full_name="Repeat Offender")
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
            for _ in range(3):
                await _log(
                    session,
                    action=ModerationAction.CHAT_AUTO_BAN,
                    target_type=ModerationTargetType.USER,
                    target_id=offender.id,
                    outcome={},
                )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/flagged-users")
        assert response.status_code == 200
        body = response.json()
        assert body["threshold"] == repeat_offender_service.REPEAT_OFFENDER_THRESHOLD
        assert body["window_days"] == repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS
        item = next(i for i in body["items"] if i["user_id"] == str(offender.id))
        assert item["recent_action_count"] == 3
        assert item["full_name"] == "Repeat Offender"

    async def test_moderator_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/flagged-users")
        assert response.status_code == 403

    async def test_senior_moderator_gets_403(self, db_session_factory, client_factory):
        """Deliberately stricter than the video/chat queues' own
        Moderator-tier inline count -- see `admin_repeat_offenders.py`'s
        own module docstring for the "platform-wide roster is Owner-only"
        reasoning."""
        async with db_session_factory() as session:
            senior_mod = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior_mod)
        response = client.get("/api/v1/admin/flagged-users")
        assert response.status_code == 403

    async def test_no_flagged_users_returns_empty_list(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/flagged-users")
        assert response.status_code == 200
        assert response.json()["items"] == []
