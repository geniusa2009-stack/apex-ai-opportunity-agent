"""Behavioral tests for Review of Guild Content: real Step 1/2
investigation confirmed (a) the Gemini safety/PII check and the general
blacklist already cover guild content automatically pre-publish, with
the blacklist half already visible via the existing Blacklist tab, and
(b) the one genuine gap was that no user could report an
already-published post/comment. This file covers the new
`app/services/guild_report_service.py` get-or-create/anti-mass-report
logic, the new `POST /guilds/posts/{post_id}/report` and
`POST /guilds/posts/{post_id}/comments/{comment_id}/report` endpoints,
the Claim System's reuse for `ModerationTargetType.GUILD_CONTENT_REPORT`
(zero new claim code), the new `admin_guild_reports.py` queue +
resolution endpoints (dismiss = Moderator, delete-content = Senior
Moderator), and `repeat_offender_service`'s new `GUILD_CONTENT_DELETE`
punitive-action wiring.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique as
`tests/test_chat_reports.py`, which this file mirrors closely.

This file does NOT touch concurrent async connections (no WebSocket
paths, same posture as `test_chat_reports.py`) -- the `StaticPool`/
aiosqlite `pool_reset_on_return=None` fix established for
`test_chat.py`/`test_security_shield.py` does not apply here, checked
proactively per this task's own instruction.
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
from app.models.enums import AdminRole, ChatReportReason, ModerationAction
from app.models.guild_moderation import GuildContentReport, GuildContentReportEntry
from app.models.guilds import Guild, GuildComment, GuildPost
from app.models.moderation_log import ModerationLogEntry
from app.models.user import User
from app.services import guild_report_service, notification_connection_manager, notification_service, repeat_offender_service

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
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))
        await conn.execute(text(_CREATE_DANGEROUS_ACTION_CONFIRMATIONS_TABLE_SQL))
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Guild.__table__,
                GuildPost.__table__,
                GuildComment.__table__,
                GuildContentReport.__table__,
                GuildContentReportEntry.__table__,
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


async def _make_guild(db: AsyncSession, *, created_by: uuid.UUID) -> Guild:
    guild = Guild(id=uuid.uuid4(), name="Test Guild", topic="Testing", created_by=created_by)
    db.add(guild)
    await db.commit()
    await db.refresh(guild)
    return guild


async def _make_post(db: AsyncSession, *, guild_id: uuid.UUID, user_id: uuid.UUID, content: str = "hello") -> GuildPost:
    post = GuildPost(id=uuid.uuid4(), guild_id=guild_id, user_id=user_id, content=content, is_anonymous=False)
    db.add(post)
    await db.commit()
    await db.refresh(post)
    return post


async def _make_comment(db: AsyncSession, *, post_id: uuid.UUID, user_id: uuid.UUID, content: str = "a comment") -> GuildComment:
    comment = GuildComment(id=uuid.uuid4(), post_id=post_id, user_id=user_id, content=content)
    db.add(comment)
    await db.commit()
    await db.refresh(comment)
    return comment


@pytest.fixture(autouse=True)
def _fake_notification_broadcast(monkeypatch):
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


def _post_report_url(post_id: uuid.UUID) -> str:
    return f"/api/v1/guilds/posts/{post_id}/report"


def _comment_report_url(post_id: uuid.UUID, comment_id: uuid.UUID) -> str:
    return f"/api/v1/guilds/posts/{post_id}/comments/{comment_id}/report"


def _claim_url(report_id: uuid.UUID) -> str:
    return f"/api/v1/admin/moderation/guild_content_report/{report_id}/claim"


# ---------------------------------------------------------------------
# Service layer: create_report (get-or-create case + anti-mass-report)
# ---------------------------------------------------------------------


class TestCreateReportService:
    pytestmark = pytest.mark.asyncio

    async def test_self_report_raises(self, db_session_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)

            with pytest.raises(guild_report_service.SelfReportError):
                await guild_report_service.create_report(
                    db, target_type="guild_post", target_id=post.id,
                    author_id=author.id, reporter_id=author.id,
                    reason=ChatReportReason.SPAM, details=None,
                )

    async def test_duplicate_report_raises(self, db_session_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)

            await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            with pytest.raises(guild_report_service.DuplicateReportError):
                await guild_report_service.create_report(
                    db, target_type="guild_post", target_id=post.id,
                    author_id=author.id, reporter_id=reporter.id,
                    reason=ChatReportReason.HARASSMENT, details=None,
                )

    async def test_two_distinct_reporters_share_one_case(self, db_session_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter1 = await _make_user(db, full_name="Reporter1")
            reporter2 = await _make_user(db, full_name="Reporter2")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)

            case1, _, total1 = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter1.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            case2, _, total2 = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter2.id,
                reason=ChatReportReason.HARASSMENT, details="context",
            )
            assert case1.id == case2.id
            assert total1 == 1
            assert total2 == 2

    async def test_posts_and_comments_are_independent_targets(self, db_session_factory):
        """A report on a post and a report on a comment belonging to
        that post must be two distinct cases -- confirms the polymorphic
        `(target_type, target_id)` discriminator actually disambiguates,
        not just coincidentally works because ids are always unique."""
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            comment = await _make_comment(db, post_id=post.id, user_id=author.id)

            post_case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            comment_case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_comment", target_id=comment.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            assert post_case.id != comment_case.id


# ---------------------------------------------------------------------
# POST /guilds/posts/{post_id}/report  +  .../comments/{id}/report
# ---------------------------------------------------------------------


class TestReportEndpoints:
    pytestmark = pytest.mark.asyncio

    async def test_report_post_success(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id, content="bad content")

        client = client_factory(reporter)
        resp = client.post(_post_report_url(post.id), json={"reason": "spam", "details": "looks fake"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["target_type"] == "guild_post"
        assert body["target_id"] == str(post.id)
        assert body["total_reports"] == 1

    async def test_report_own_post_returns_400(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)

        client = client_factory(author)
        resp = client.post(_post_report_url(post.id), json={"reason": "spam"})
        assert resp.status_code == 400

    async def test_duplicate_report_returns_409(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)

        client = client_factory(reporter)
        first = client.post(_post_report_url(post.id), json={"reason": "spam"})
        assert first.status_code == 201
        second = client.post(_post_report_url(post.id), json={"reason": "harassment"})
        assert second.status_code == 409

    async def test_report_nonexistent_post_returns_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            reporter = await _make_user(db, full_name="Reporter")

        client = client_factory(reporter)
        resp = client.post(_post_report_url(uuid.uuid4()), json={"reason": "spam"})
        assert resp.status_code == 404

    async def test_report_comment_success(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            comment = await _make_comment(db, post_id=post.id, user_id=author.id)

        client = client_factory(reporter)
        resp = client.post(_comment_report_url(post.id, comment.id), json={"reason": "inappropriate_content"})
        assert resp.status_code == 201
        body = resp.json()
        assert body["target_type"] == "guild_comment"
        assert body["target_id"] == str(comment.id)

    async def test_comment_belonging_to_different_post_returns_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            guild = await _make_guild(db, created_by=author.id)
            post1 = await _make_post(db, guild_id=guild.id, user_id=author.id)
            post2 = await _make_post(db, guild_id=guild.id, user_id=author.id)
            comment = await _make_comment(db, post_id=post1.id, user_id=author.id)

        client = client_factory(reporter)
        resp = client.post(_comment_report_url(post2.id, comment.id), json={"reason": "spam"})
        assert resp.status_code == 404


# ---------------------------------------------------------------------
# GET /admin/guild/reports  (Moderator+)
# ---------------------------------------------------------------------


class TestGuildReportsQueue:
    pytestmark = pytest.mark.asyncio

    async def test_student_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db, full_name="Student")

        client = client_factory(student)
        resp = client.get("/api/v1/admin/guild/reports")
        assert resp.status_code == 403

    async def test_moderator_gets_200_empty(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")

        client = client_factory(moderator)
        resp = client.get("/api/v1/admin/guild/reports")
        assert resp.status_code == 200
        assert resp.json()["items"] == []

    async def test_open_case_appears_with_real_embedded_content_and_entries(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id, content="the real post text")
            await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.HARASSMENT, details="it's mean",
            )
            await db.commit()

        client = client_factory(moderator)
        resp = client.get("/api/v1/admin/guild/reports?status=open")
        assert resp.status_code == 200
        items = resp.json()["items"]
        assert len(items) == 1
        item = items[0]
        assert item["content"]["content"] == "the real post text"
        assert item["content"]["author_id"] == str(author.id)
        assert item["report_count"] == 1
        assert item["reports"][0]["reason"] == "harassment"
        assert item["reports"][0]["details"] == "it's mean"

    async def test_status_filter_excludes_open_when_asking_dismissed(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client = client_factory(moderator)
        resp = client.get("/api/v1/admin/guild/reports?status=dismissed")
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------
# POST /admin/guild/reports/{report_id}/dismiss  (Moderator+)
# ---------------------------------------------------------------------


class TestDismissGuildReport:
    pytestmark = pytest.mark.asyncio

    async def test_moderator_can_dismiss(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client = client_factory(moderator)
        resp = client.post(f"/api/v1/admin/guild/reports/{case.id}/dismiss", json={"notes": "false alarm"})
        assert resp.status_code == 200
        assert resp.json()["status"] == "dismissed"

    async def test_dismissing_already_resolved_case_returns_409(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client = client_factory(moderator)
        first = client.post(f"/api/v1/admin/guild/reports/{case.id}/dismiss", json={})
        assert first.status_code == 200
        second = client.post(f"/api/v1/admin/guild/reports/{case.id}/dismiss", json={})
        assert second.status_code == 409

    async def test_re_reporting_a_dismissed_post_opens_a_fresh_case(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter1 = await _make_user(db, full_name="Reporter1")
            reporter2 = await _make_user(db, full_name="Reporter2")
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            case1, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter1.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client_mod = client_factory(moderator)
        dismiss_resp = client_mod.post(f"/api/v1/admin/guild/reports/{case1.id}/dismiss", json={})
        assert dismiss_resp.status_code == 200

        client_reporter2 = client_factory(reporter2)
        report_resp = client_reporter2.post(_post_report_url(post.id), json={"reason": "harassment"})
        assert report_resp.status_code == 201
        assert report_resp.json()["report_id"] != str(case1.id)


# ---------------------------------------------------------------------
# DELETE /admin/guild/reports/{report_id}/content  (Senior Moderator+)
# ---------------------------------------------------------------------


class TestDeleteReportedGuildContent:
    pytestmark = pytest.mark.asyncio

    async def test_moderator_cannot_delete(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client = client_factory(moderator)
        resp = client.delete(f"/api/v1/admin/guild/reports/{case.id}/content")
        assert resp.status_code == 403

    async def test_senior_moderator_deletes_post_and_logs_author(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Senior")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id, content="delete me")
            case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.INAPPROPRIATE_CONTENT, details=None,
            )
            await db.commit()

        client = client_factory(senior)
        resp = client.request("DELETE", f"/api/v1/admin/guild/reports/{case.id}/content", json={"notes": "clear violation"})
        assert resp.status_code == 202
        confirmation_id = resp.json()["confirmation_id"]

        async with db_session_factory() as db:
            second_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Second Senior")
        approver_client = client_factory(second_senior)
        approve_resp = approver_client.post(f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve")
        assert approve_resp.status_code == 200

        async with db_session_factory() as db:
            deleted_post = await db.get(GuildPost, post.id)
            assert deleted_post is None

            result = await db.execute(
                select(ModerationLogEntry).where(ModerationLogEntry.action == "guild_content_delete")
            )
            entries = list(result.scalars().all())
            assert len(entries) == 1
            assert entries[0].outcome["author_id"] == str(author.id)

    async def test_reporting_a_comment_then_deleting_removes_only_the_comment(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Senior")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id, content="the post stays")
            comment = await _make_comment(db, post_id=post.id, user_id=author.id, content="the comment goes")
            case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_comment", target_id=comment.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client = client_factory(senior)
        resp = client.delete(f"/api/v1/admin/guild/reports/{case.id}/content")
        assert resp.status_code == 202
        confirmation_id = resp.json()["confirmation_id"]

        async with db_session_factory() as db:
            second_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Second Senior")
        approver_client = client_factory(second_senior)
        approve_resp = approver_client.post(f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve")
        assert approve_resp.status_code == 200

        async with db_session_factory() as db:
            assert await db.get(GuildComment, comment.id) is None
            assert await db.get(GuildPost, post.id) is not None

    async def test_deleting_already_resolved_case_returns_409(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Senior")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client = client_factory(senior)
        first = client.delete(f"/api/v1/admin/guild/reports/{case.id}/content")
        assert first.status_code == 202
        confirmation_id = first.json()["confirmation_id"]

        async with db_session_factory() as db:
            second_senior = await _make_user(db, admin_role=AdminRole.SENIOR_MODERATOR, full_name="Second Senior")
        approver_client = client_factory(second_senior)
        approve_resp = approver_client.post(f"/api/v1/admin/moderation/dangerous-actions/{confirmation_id}/approve")
        assert approve_resp.status_code == 200

        # Real Two-Person Confirmation for Dangerous Actions addition:
        # the case is only actually resolved (content_deleted) once the
        # second approval runs -- so the honest 409-on-already-resolved
        # check is now exercised by a THIRD delete call, after approval,
        # not the second.
        third = client.delete(f"/api/v1/admin/guild/reports/{case.id}/content")
        assert third.status_code == 409


# ---------------------------------------------------------------------
# Claim System reuse -- zero new claim code
# ---------------------------------------------------------------------


class TestGuildContentReportClaimReuse:
    pytestmark = pytest.mark.asyncio

    async def test_claim_and_release_work_via_existing_endpoints(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")
            reporter = await _make_user(db, full_name="Reporter")
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod")
            guild = await _make_guild(db, created_by=author.id)
            post = await _make_post(db, guild_id=guild.id, user_id=author.id)
            case, _, _ = await guild_report_service.create_report(
                db, target_type="guild_post", target_id=post.id,
                author_id=author.id, reporter_id=reporter.id,
                reason=ChatReportReason.SPAM, details=None,
            )
            await db.commit()

        client = client_factory(moderator)

        claim_resp = client.post(_claim_url(case.id))
        assert claim_resp.status_code == 200
        assert claim_resp.json()["claimed_by"] == str(moderator.id)

        release_resp = client.delete(_claim_url(case.id))
        assert release_resp.status_code == 204


# ---------------------------------------------------------------------
# Repeat-Offender Detection reuse -- GUILD_CONTENT_DELETE is punitive
# ---------------------------------------------------------------------


class TestGuildContentDeleteCountsAsOffense:
    pytestmark = pytest.mark.asyncio

    async def test_get_offense_events_since_includes_guild_content_delete(self, db_session_factory):
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")

            db.add(
                ModerationLogEntry(
                    id=uuid.uuid4(), actor_id=None, actor_role=None,
                    action=ModerationAction.GUILD_CONTENT_DELETE, target_type="guild_content_report",
                    target_id=uuid.uuid4(), outcome={"author_id": str(author.id)},
                )
            )
            await db.commit()

            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
            events = await repeat_offender_service.get_offense_events_since(db, cutoff)
            offender_ids = [oid for oid, _ in events]
            assert author.id in offender_ids

    async def test_dismissed_guild_report_does_not_count_as_offense(self, db_session_factory):
        """A dismissed report means a human reviewer decided the report
        was baseless -- must NOT count against the content's real
        author, same reasoning `CHAT_REPORT_DISMISS`'s own exclusion
        already established."""
        async with db_session_factory() as db:
            author = await _make_user(db, full_name="Author")

            db.add(
                ModerationLogEntry(
                    id=uuid.uuid4(), actor_id=None, actor_role=None,
                    action=ModerationAction.GUILD_REPORT_DISMISS, target_type="guild_content_report",
                    target_id=uuid.uuid4(), outcome={"notes": None},
                )
            )
            await db.commit()

            cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=1)
            events = await repeat_offender_service.get_offense_events_since(db, cutoff)
            assert events == []
