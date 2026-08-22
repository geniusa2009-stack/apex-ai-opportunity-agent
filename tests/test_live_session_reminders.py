"""Behavioral tests for the Live 1:1 Video Sessions reminder scan:
`app/workers/live_session_reminder_tasks._check_upcoming_live_sessions`.

Exercises the async body directly (not the `@celery_app.task`-wrapped
`check_upcoming_live_sessions_task` sync entrypoint) -- same posture as
this codebase would use for `deadline_reminder_tasks._check_approaching_
deadlines` (no existing test file covers that one, but the shape a test
SHOULD take is the same): the Celery task wrapper is a one-line
`asyncio.run(...)` shim with nothing of its own worth testing, and
invoking it directly here would require a real/patched Celery app
context for no behavioral benefit.

In-memory SQLite, `StaticPool`, same established discipline as `tests/
test_live_sessions.py` (this feature's own closest sibling, and the
table this one reads from): `users`/`user_bookmarks` via raw SQL
(`User.bookmarks` is `lazy="selectin"` -- see that file's own docstring
for the full reasoning), `live_sessions`/`notifications` created
directly off the real ORM metadata (`Base.metadata.create_all`) since
neither `LiveSession` nor `Notification` has any Postgres-specific
column type.

No Redis/Celery broker is monkeypatched here, matching `tests/
test_notifications.py`'s own established posture: `notification_service.
create_notification`'s real-time-broadcast and critical-email steps are
BOTH wrapped in their own best-effort `try/except` (see that module's own
docstring) -- an unreachable Redis/Celery broker in this sandbox degrades
those two steps to a logged warning, it does not raise, so the DB-level
assertions this file cares about (was a `Notification` row actually
written, was `reminder_sent` actually flipped) are unaffected either way.
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.core.config import settings
from app.db.base import Base
from app.models.enums import LiveSessionStatus
from app.models.live_session import LiveSession
from app.models.notification import Notification
from app.workers.live_session_reminder_tasks import _check_upcoming_live_sessions

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
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[LiveSession.__table__, Notification.__table__],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(session: AsyncSession, *, full_name: str) -> uuid.UUID:
    user_id = uuid.uuid4()
    await session.execute(
        text(
            "INSERT INTO users (id, email, full_name, is_active, is_superuser) "
            "VALUES (:id, :email, :full_name, 1, 0)"
        ),
        {"id": str(user_id), "email": f"{user_id}@example.com", "full_name": full_name},
    )
    await session.commit()
    return user_id


async def _make_live_session(
    session: AsyncSession,
    *,
    requested_by_id: uuid.UUID,
    requested_with_id: uuid.UUID,
    scheduled_at: datetime,
    status: str = LiveSessionStatus.CONFIRMED.value,
    reminder_sent: bool = False,
) -> LiveSession:
    live_session = LiveSession(
        requested_by_id=requested_by_id,
        requested_with_id=requested_with_id,
        status=status,
        scheduled_at=scheduled_at,
        reminder_sent=reminder_sent,
    )
    session.add(live_session)
    await session.commit()
    await session.refresh(live_session)
    return live_session


# ---------------------------------------------------------------------
# The reminder scan itself
# ---------------------------------------------------------------------


class TestLiveSessionReminderScan:
    async def test_session_within_window_gets_both_parties_notified(self, db_session_factory):
        async with db_session_factory() as session:
            requester = await _make_user(session, full_name="Requester")
            counterparty = await _make_user(session, full_name="Counterparty")
            now = datetime.now(timezone.utc)
            live_session = await _make_live_session(
                session,
                requested_by_id=requester,
                requested_with_id=counterparty,
                scheduled_at=now + timedelta(minutes=30),
            )

        import app.workers.live_session_reminder_tasks as task_module

        original_factory = task_module.AsyncSessionLocal
        task_module.AsyncSessionLocal = db_session_factory
        try:
            summary = await _check_upcoming_live_sessions()
        finally:
            task_module.AsyncSessionLocal = original_factory

        assert summary == {"checked": 1, "notified": 1, "errors": 0}

        async with db_session_factory() as session:
            refreshed = await session.get(LiveSession, live_session.id)
            assert refreshed.reminder_sent is True

            notifications = (await session.execute(select(Notification))).scalars().all()
            assert len(notifications) == 2
            notified_user_ids = {n.user_id for n in notifications}
            assert notified_user_ids == {requester, counterparty}
            assert all(n.type == "WARNING" for n in notifications)
            assert all("starts in" in n.message for n in notifications)

    async def test_session_outside_window_is_untouched(self, db_session_factory):
        async with db_session_factory() as session:
            requester = await _make_user(session, full_name="Requester")
            counterparty = await _make_user(session, full_name="Counterparty")
            now = datetime.now(timezone.utc)
            # Well beyond `live_session_reminder_minutes_before` (default 60).
            far_future_session = await _make_live_session(
                session,
                requested_by_id=requester,
                requested_with_id=counterparty,
                scheduled_at=now + timedelta(hours=6),
            )

        import app.workers.live_session_reminder_tasks as task_module

        original_factory = task_module.AsyncSessionLocal
        task_module.AsyncSessionLocal = db_session_factory
        try:
            summary = await _check_upcoming_live_sessions()
        finally:
            task_module.AsyncSessionLocal = original_factory

        assert summary == {"checked": 0, "notified": 0, "errors": 0}

        async with db_session_factory() as session:
            refreshed = await session.get(LiveSession, far_future_session.id)
            assert refreshed.reminder_sent is False
            notifications = (await session.execute(select(Notification))).scalars().all()
            assert notifications == []

    async def test_already_reminded_session_is_not_re_notified(self, db_session_factory):
        async with db_session_factory() as session:
            requester = await _make_user(session, full_name="Requester")
            counterparty = await _make_user(session, full_name="Counterparty")
            now = datetime.now(timezone.utc)
            await _make_live_session(
                session,
                requested_by_id=requester,
                requested_with_id=counterparty,
                scheduled_at=now + timedelta(minutes=15),
                reminder_sent=True,
            )

        import app.workers.live_session_reminder_tasks as task_module

        original_factory = task_module.AsyncSessionLocal
        task_module.AsyncSessionLocal = db_session_factory
        try:
            summary = await _check_upcoming_live_sessions()
        finally:
            task_module.AsyncSessionLocal = original_factory

        assert summary == {"checked": 0, "notified": 0, "errors": 0}

        async with db_session_factory() as session:
            notifications = (await session.execute(select(Notification))).scalars().all()
            assert notifications == []

    async def test_non_confirmed_session_within_window_is_ignored(self, db_session_factory):
        async with db_session_factory() as session:
            requester = await _make_user(session, full_name="Requester")
            counterparty = await _make_user(session, full_name="Counterparty")
            now = datetime.now(timezone.utc)
            await _make_live_session(
                session,
                requested_by_id=requester,
                requested_with_id=counterparty,
                scheduled_at=now + timedelta(minutes=20),
                status=LiveSessionStatus.REQUESTED.value,
            )

        import app.workers.live_session_reminder_tasks as task_module

        original_factory = task_module.AsyncSessionLocal
        task_module.AsyncSessionLocal = db_session_factory
        try:
            summary = await _check_upcoming_live_sessions()
        finally:
            task_module.AsyncSessionLocal = original_factory

        assert summary == {"checked": 0, "notified": 0, "errors": 0}

        async with db_session_factory() as session:
            notifications = (await session.execute(select(Notification))).scalars().all()
            assert notifications == []

    async def test_past_session_is_ignored(self, db_session_factory):
        async with db_session_factory() as session:
            requester = await _make_user(session, full_name="Requester")
            counterparty = await _make_user(session, full_name="Counterparty")
            now = datetime.now(timezone.utc)
            await _make_live_session(
                session,
                requested_by_id=requester,
                requested_with_id=counterparty,
                scheduled_at=now - timedelta(minutes=5),
            )

        import app.workers.live_session_reminder_tasks as task_module

        original_factory = task_module.AsyncSessionLocal
        task_module.AsyncSessionLocal = db_session_factory
        try:
            summary = await _check_upcoming_live_sessions()
        finally:
            task_module.AsyncSessionLocal = original_factory

        assert summary == {"checked": 0, "notified": 0, "errors": 0}

    async def test_reminder_window_setting_is_respected(self, db_session_factory, monkeypatch):
        """A session just OUTSIDE a narrowed window is skipped; the exact
        same session becomes eligible once the window is widened to cover
        it -- proves the scan genuinely reads `settings.live_session_
        reminder_minutes_before` at call time rather than a hardcoded
        value."""
        async with db_session_factory() as session:
            requester = await _make_user(session, full_name="Requester")
            counterparty = await _make_user(session, full_name="Counterparty")
            now = datetime.now(timezone.utc)
            live_session = await _make_live_session(
                session,
                requested_by_id=requester,
                requested_with_id=counterparty,
                scheduled_at=now + timedelta(minutes=45),
            )

        import app.workers.live_session_reminder_tasks as task_module

        monkeypatch.setattr(settings, "live_session_reminder_minutes_before", 10)
        original_factory = task_module.AsyncSessionLocal
        task_module.AsyncSessionLocal = db_session_factory
        try:
            summary = await _check_upcoming_live_sessions()
            assert summary == {"checked": 0, "notified": 0, "errors": 0}

            monkeypatch.setattr(settings, "live_session_reminder_minutes_before", 60)
            summary = await _check_upcoming_live_sessions()
            assert summary == {"checked": 1, "notified": 1, "errors": 0}
        finally:
            task_module.AsyncSessionLocal = original_factory

        async with db_session_factory() as session:
            refreshed = await session.get(LiveSession, live_session.id)
            assert refreshed.reminder_sent is True
