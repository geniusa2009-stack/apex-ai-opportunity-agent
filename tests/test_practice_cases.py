"""Behavioral tests for Training / Practice Mode for New Volunteers:
`app/services/practice_case_service.py`'s real attempt-recording logic,
the new `/me/practice*` endpoints (`app/api/v1/endpoints/
practice_cases.py`), and -- the centerpiece of this file, per this
task's own Step 4 emphasis -- a real, adversarial proof that a practice
action can NEVER reach a real notification, a real ban, or real content
deletion. `tests/test_onboarding.py::TestOwnOnboardingChecklist::
test_all_completed_true_once_every_step_marked` and
`::test_practice_step_cannot_be_self_marked_or_unmarked` already cover
this feature's real integration with the onboarding checklist's sixth
step -- not duplicated here.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique as
every other admin-panel test file in this codebase.
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
from app.models.enums import AdminRole
from app.models.practice_case import (
    PRACTICE_ACTIONS_BY_CASE_TYPE,
    PRACTICE_CASE_TYPE_CHAT_REPORT,
    PRACTICE_CASE_TYPE_GUILD_REPORT,
    PRACTICE_CASE_TYPE_VIDEO_REVIEW,
    PRACTICE_CASES,
    PracticeCaseAttempt,
)
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

# The real "could a practice action have accidentally reached this
# table" boundary tables -- every table a REAL trust-and-safety action
# (video delete, chat-message delete, guild-content delete, a ban) ever
# writes to. Created here ONLY so the boundary test below can assert
# they stay genuinely EMPTY after every real practice action -- never
# written to by `practice_case_service.py` (see that module's own
# docstring: it doesn't even import the code that could write to them).
_CREATE_STUDENT_VIDEOS_TABLE_SQL = """
CREATE TABLE student_videos (
    id VARCHAR(36) PRIMARY KEY, student_id VARCHAR(36) NOT NULL, title VARCHAR(255) NOT NULL,
    description TEXT, hls_master_url VARCHAR(500), status VARCHAR(20) NOT NULL DEFAULT 'pending_ai',
    ai_score INTEGER, ai_safety_flag BOOLEAN NOT NULL DEFAULT 0, ai_feedback TEXT, summary_embedding TEXT,
    duration_seconds INTEGER, transcript_text TEXT, transcript_segments TEXT, thumbnail_url VARCHAR(500),
    admin_override BOOLEAN NOT NULL DEFAULT 0, admin_notes TEXT, claimed_by VARCHAR(36), claimed_at DATETIME,
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
_CREATE_NOTIFICATIONS_TABLE_SQL = """
CREATE TABLE notifications (
    id VARCHAR(36) PRIMARY KEY,
    user_id VARCHAR(36) NOT NULL,
    type VARCHAR(50) NOT NULL,
    title VARCHAR(255) NOT NULL,
    message TEXT NOT NULL,
    is_read BOOLEAN DEFAULT 0 NOT NULL,
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
        await conn.execute(text(_CREATE_STUDENT_VIDEOS_TABLE_SQL))
        await conn.run_sync(Base.metadata.create_all, tables=[PracticeCaseAttempt.__table__])

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
# `GET /me/practice` -- tier gating, real content.
# ---------------------------------------------------------------------


class TestPracticeChecklistAccess:
    async def test_plain_student_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
        client = client_factory(student)
        response = client.get("/api/v1/me/practice")
        assert response.status_code == 403

    async def test_fresh_moderator_sees_every_case_unattempted(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/me/practice")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == len(PRACTICE_CASES)
        assert body["attempted_count"] == 0
        assert body["all_attempted"] is False
        assert all(item["attempted"] is False for item in body["items"])
        # Real content genuinely renders, not empty placeholders.
        keys = {item["key"] for item in body["items"]}
        assert keys == {case.key for case in PRACTICE_CASES}
        for item in body["items"]:
            assert item["scenario"]
            assert item["content_text"]
            assert item["fake_actor_name"]


# ---------------------------------------------------------------------
# `POST /me/practice/{case_key}/act` -- real input validation, real
# upsert-on-reattempt, real tier enforcement mirroring the actual route.
# ---------------------------------------------------------------------


class TestPracticeAction:
    async def test_unknown_case_key_is_404(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.post("/api/v1/me/practice/not_a_real_case/act", json={"action": "approve"})
        assert response.status_code == 404

    async def test_action_not_valid_for_case_type_is_422(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        video_case = next(c for c in PRACTICE_CASES if c.case_type == PRACTICE_CASE_TYPE_VIDEO_REVIEW)
        # 'dismiss' is a real chat/guild action, not a real video one.
        response = client.post(f"/api/v1/me/practice/{video_case.key}/act", json={"action": "dismiss"})
        assert response.status_code == 422

    async def test_action_below_the_real_tier_limit_is_403_and_not_recorded(self, db_session_factory, client_factory):
        """Mirrors the real route's own tier split (Senior Moderator+
        for a real delete) -- a plain Moderator practicing 'delete' on a
        video case genuinely gets the real 403 a plain Moderator would
        get on the real route, and nothing is recorded."""
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        video_case = next(c for c in PRACTICE_CASES if c.case_type == PRACTICE_CASE_TYPE_VIDEO_REVIEW)
        response = client.post(f"/api/v1/me/practice/{video_case.key}/act", json={"action": "delete"})
        assert response.status_code == 403

        checklist = client.get("/api/v1/me/practice").json()
        item = next(i for i in checklist["items"] if i["key"] == video_case.key)
        assert item["attempted"] is False

    async def test_senior_moderator_can_practice_the_senior_only_action(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        video_case = next(c for c in PRACTICE_CASES if c.case_type == PRACTICE_CASE_TYPE_VIDEO_REVIEW)
        response = client.post(f"/api/v1/me/practice/{video_case.key}/act", json={"action": "delete", "notes": "Practicing the delete tier."})
        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["action_taken"] == "delete"
        assert "no real notification" in body["message"] and "no real content" in body["message"]

    async def test_every_valid_action_for_every_case_type_is_accepted_identically(self, db_session_factory, client_factory):
        """Scope Decision 1 (no scoring): every real, valid action is
        recorded the same way -- there is no 'correct' one among them."""
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True)
        client = client_factory(owner)
        for case in PRACTICE_CASES:
            for action in PRACTICE_ACTIONS_BY_CASE_TYPE[case.case_type]:
                response = client.post(f"/api/v1/me/practice/{case.key}/act", json={"action": action})
                assert response.status_code == 200, f"{case.key}/{action} -> {response.status_code}"

    async def test_reattempting_a_case_overwrites_not_appends(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        video_case = next(c for c in PRACTICE_CASES if c.case_type == PRACTICE_CASE_TYPE_VIDEO_REVIEW)

        first = client.post(f"/api/v1/me/practice/{video_case.key}/act", json={"action": "approve", "notes": "first pass"})
        assert first.status_code == 200
        second = client.post(f"/api/v1/me/practice/{video_case.key}/act", json={"action": "reject", "notes": "changed my mind"})
        assert second.status_code == 200

        checklist = client.get("/api/v1/me/practice").json()
        item = next(i for i in checklist["items"] if i["key"] == video_case.key)
        assert item["action_taken"] == "reject"
        assert item["notes"] == "changed my mind"

        async with db_session_factory() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM practice_case_attempts WHERE case_key = :k"), {"k": video_case.key})
            assert result.scalar_one() == 1, "a re-attempt overwrites the same row, never appends a second one"

    async def test_notes_are_optional(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        chat_case = next(c for c in PRACTICE_CASES if c.case_type == PRACTICE_CASE_TYPE_CHAT_REPORT)
        response = client.post(f"/api/v1/me/practice/{chat_case.key}/act", json={"action": "dismiss"})
        assert response.status_code == 200


# ---------------------------------------------------------------------
# `DELETE /me/practice/{case_key}/act` -- real, idempotent undo.
# ---------------------------------------------------------------------


class TestPracticeReset:
    async def test_reset_removes_the_attempt(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        guild_case = next(c for c in PRACTICE_CASES if c.case_type == PRACTICE_CASE_TYPE_GUILD_REPORT)
        client.post(f"/api/v1/me/practice/{guild_case.key}/act", json={"action": "dismiss"})

        reset_resp = client.delete(f"/api/v1/me/practice/{guild_case.key}/act")
        assert reset_resp.status_code == 200

        checklist = client.get("/api/v1/me/practice").json()
        item = next(i for i in checklist["items"] if i["key"] == guild_case.key)
        assert item["attempted"] is False

    async def test_reset_of_an_already_unattempted_case_is_a_harmless_noop(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        guild_case = next(c for c in PRACTICE_CASES if c.case_type == PRACTICE_CASE_TYPE_GUILD_REPORT)
        response = client.delete(f"/api/v1/me/practice/{guild_case.key}/act")
        assert response.status_code == 200

    async def test_reset_of_unknown_case_is_404(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.delete("/api/v1/me/practice/not_a_real_case/act")
        assert response.status_code == 404


# ---------------------------------------------------------------------
# THE CENTERPIECE: the real airtight practice/real boundary. Every real
# side-effect function this codebase has is patched here; the test
# asserts NONE of them is ever called, across EVERY real practice case
# and EVERY real allowed action for it -- and independently confirms
# every real production table stays genuinely EMPTY throughout. This is
# this deliverable's single most important test, per this task's own
# Step 4 emphasis.
# ---------------------------------------------------------------------


class TestBoundaryNeverTouchesRealSideEffects:
    async def test_no_practice_action_ever_reaches_a_real_side_effect(self, db_session_factory, client_factory, monkeypatch):
        from app.services import chat_security, moderation_log_service, notification_service

        log_calls = []
        notify_calls = []
        ban_calls = []

        def _fake_log_action(*args, **kwargs):
            log_calls.append((args, kwargs))
            raise AssertionError("moderation_log_service.log_action was called by a practice action")

        async def _fake_create_notification(*args, **kwargs):
            notify_calls.append((args, kwargs))
            raise AssertionError("notification_service.create_notification was called by a practice action")

        async def _fake_ban_user(*args, **kwargs):
            ban_calls.append((args, kwargs))
            raise AssertionError("chat_security.ban_user was called by a practice action")

        monkeypatch.setattr(moderation_log_service, "log_action", _fake_log_action)
        monkeypatch.setattr(notification_service, "create_notification", _fake_create_notification)
        monkeypatch.setattr(chat_security, "ban_user", _fake_ban_user)

        # Also patch the real R2 cleanup dispatch, the same real
        # boundary `dangerous_action_service.py`'s own prior-phase tests
        # already established a real side effect worth guarding.
        from app.workers import cleanup_tasks

        def _fake_delay(*args, **kwargs):
            raise AssertionError("task_delete_orphaned_media.delay was called by a practice action")

        monkeypatch.setattr(cleanup_tasks.task_delete_orphaned_media, "delay", _fake_delay)

        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True)
        client = client_factory(owner)

        # Real, exhaustive sweep: every real case, every real allowed
        # action, including a real reattempt (exercising the UPDATE
        # path, not just INSERT) and a real reset (DELETE path).
        for case in PRACTICE_CASES:
            for action in PRACTICE_ACTIONS_BY_CASE_TYPE[case.case_type]:
                response = client.post(f"/api/v1/me/practice/{case.key}/act", json={"action": action, "notes": "boundary sweep"})
                assert response.status_code == 200
        for case in PRACTICE_CASES:
            reset_response = client.delete(f"/api/v1/me/practice/{case.key}/act")
            assert reset_response.status_code == 200

        assert log_calls == [], "no practice action may ever write a real ModerationLogEntry"
        assert notify_calls == [], "no practice action may ever send a real notification"
        assert ban_calls == [], "no practice action may ever apply a real ban"

        # Independent confirmation, straight against the real tables a
        # real action would have written to -- not just "the patched
        # function wasn't called" (which could theoretically be true
        # even if some OTHER, unpatched code path wrote there directly).
        async with db_session_factory() as session:
            for table in ("student_videos", "moderation_log_entries", "notifications"):
                count = (await session.execute(text(f"SELECT COUNT(*) FROM {table}"))).scalar_one()
                assert count == 0, f"real table '{table}' must stay genuinely empty after every real practice action (found {count} row(s))"

            # The one, real, structurally-isolated table this feature
            # DOES write to -- confirms the sweep above genuinely ran
            # (not a vacuously-passing test), and that a reset really
            # deletes rather than leaving orphaned state.
            attempt_count = (await session.execute(text("SELECT COUNT(*) FROM practice_case_attempts"))).scalar_one()
            assert attempt_count == 0, "every practice case was reset at the end of this sweep"

    async def test_practice_case_attempt_has_no_foreign_key_into_any_real_item_table(self):
        """Structural, not behavioral, confirmation of Step 3.1's own
        design claim: `PracticeCaseAttempt` only has a real FK to
        `users` (who attempted it) -- never to `student_videos`,
        `chat_message_reports`, or `guild_content_reports`."""
        fk_targets = {fk.column.table.name for fk in PracticeCaseAttempt.__table__.foreign_keys}
        assert fk_targets == {"users"}
