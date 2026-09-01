"""Behavioral tests for General Content Blacklist / Pattern Auto-Trigger:
`app/models/blacklist.py`, `app/services/blacklist_service.py`'s real
matching/CRUD/logging logic, `app/api/v1/endpoints/admin_blacklist.py`'s
tier-gated CRUD + trigger-events endpoints, and real blocking behavior
at the student-video-upload, profile-bio, and guild-post pathways.

Real chat-message blocking is covered in `tests/test_chat.py` (extended
in this task, alongside the existing WebSocket infrastructure it already
has -- see that file's own new tests, right after
`test_arabic_message_bans_the_sender_and_closes_the_connection`).

Same real-HTTP-layer technique as `tests/test_moderation_log.py`
(`client_factory`), in-memory SQLite via `StaticPool`.
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
from app.models.blacklist import BlacklistedPattern
from app.models.enums import AdminRole
from app.models.guilds import Guild, GuildPost
from app.models.user import User
from app.services import blacklist_service, guild_service

pytestmark = pytest.mark.asyncio

_CREATE_USERS_TABLE_SQL = """
CREATE TABLE users (
    id VARCHAR(36) PRIMARY KEY, email VARCHAR(255) UNIQUE, hashed_password VARCHAR(255),
    full_name VARCHAR(255), is_active BOOLEAN NOT NULL DEFAULT 1, is_superuser BOOLEAN NOT NULL DEFAULT 0,
    phone VARCHAR(50), degree_level VARCHAR(100), major VARCHAR(255), gpa FLOAT, graduation_year INTEGER,
    skills TEXT, target_country VARCHAR(120), age_group VARCHAR(50), age INTEGER, academic_level VARCHAR(20),
    gender VARCHAR(10), bio TEXT, search_vector TEXT, role VARCHAR(20) NOT NULL DEFAULT 'student',
    admin_role VARCHAR(20), created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_CREATE_USER_BOOKMARKS_TABLE_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY, actor_id VARCHAR(36), actor_role VARCHAR(20), action VARCHAR(30) NOT NULL,
    target_type VARCHAR(30) NOT NULL, target_id VARCHAR(36) NOT NULL, outcome TEXT NOT NULL,
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
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[BlacklistedPattern.__table__, Guild.__table__, GuildPost.__table__],
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
# Matching mechanism -- direct, service-level unit tests (no I/O beyond
# the DB read, matching `chat_security.contains_arabic`'s own posture of
# being an independently-testable predicate).
# ---------------------------------------------------------------------


class TestMatching:
    async def test_no_patterns_never_matches(self, db_session_factory):
        async with db_session_factory() as db:
            assert await blacklist_service.check_content(db, "anything at all") is None

    async def test_none_content_never_matches(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
            assert await blacklist_service.check_content(db, None) is None

    async def test_empty_content_never_matches(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
            assert await blacklist_service.check_content(db, "") is None

    async def test_exact_word_match(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
            match = await blacklist_service.check_content(db, "you are a badword honestly")
            assert match is not None
            assert match.pattern_text == "badword"

    async def test_case_insensitive(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
            assert await blacklist_service.check_content(db, "BadWord in the middle") is not None

    async def test_word_boundary_avoids_substring_false_positive(self, db_session_factory):
        """The exact scenario `BlacklistedPattern`'s own docstring names:
        blacklisting "ass" must not silently reject "class"/"assignment"."""
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="ass"))
            await db.commit()
            assert await blacklist_service.check_content(db, "see you in class for the assignment") is None

    async def test_word_boundary_still_matches_the_word_on_its_own(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="ass"))
            await db.commit()
            assert await blacklist_service.check_content(db, "you are an ass") is not None

    async def test_regex_opt_in_matches_leetspeak_variants(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern=r"b[a4]dw[o0]rd", is_regex=True))
            await db.commit()
            assert await blacklist_service.check_content(db, "that is a B4DW0RD here") is not None

    async def test_malformed_regex_is_skipped_not_crashed(self, db_session_factory):
        """An Owner-authored typo in a regex pattern must never take
        down every real content-creation pathway on the platform."""
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="(unclosed", is_regex=True))
            await db.commit()
            assert await blacklist_service.check_content(db, "(unclosed parenthesis in real text") is None

    async def test_a_malformed_pattern_does_not_block_a_later_valid_pattern_from_matching(self, db_session_factory):
        async with db_session_factory() as db:
            db.add(BlacklistedPattern(pattern="(unclosed", is_regex=True))
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
            match = await blacklist_service.check_content(db, "this has a badword in it")
            assert match is not None
            assert match.pattern_text == "badword"


# ---------------------------------------------------------------------
# Pattern CRUD -- Owner-tier.
# ---------------------------------------------------------------------


class TestPatternCrud:
    async def test_moderator_403_on_list(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/blacklist/patterns")
        assert response.status_code == 403

    async def test_moderator_403_on_create(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.post("/api/v1/admin/blacklist/patterns", json={"pattern": "badword"})
        assert response.status_code == 403

    async def test_owner_can_create_list_and_delete(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)

        create_res = client.post(
            "/api/v1/admin/blacklist/patterns",
            json={"pattern": "badword", "is_regex": False, "description": "test entry"},
        )
        assert create_res.status_code == 201
        created = create_res.json()
        assert created["pattern"] == "badword"
        assert created["created_by"] == str(owner.id)

        list_res = client.get("/api/v1/admin/blacklist/patterns")
        assert list_res.status_code == 200
        assert len(list_res.json()) == 1

        delete_res = client.delete(f"/api/v1/admin/blacklist/patterns/{created['id']}")
        assert delete_res.status_code == 204

        list_after = client.get("/api/v1/admin/blacklist/patterns")
        assert list_after.json() == []

    async def test_delete_nonexistent_pattern_is_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.delete(f"/api/v1/admin/blacklist/patterns/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_pattern_too_long_is_422(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.post("/api/v1/admin/blacklist/patterns", json={"pattern": "x" * 201})
        assert response.status_code == 422


# ---------------------------------------------------------------------
# Trigger-events view -- Moderator+ (deliberately NOT Owner-only, see
# `admin_blacklist.py`'s own module docstring).
# ---------------------------------------------------------------------


class TestTriggerEventsView:
    async def test_moderator_can_view_trigger_events(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            student = await _make_user(db)
            match = blacklist_service.BlacklistMatch(pattern_id=uuid.uuid4(), pattern_text="badword")
            blacklist_service.log_trigger(
                db, submitting_user_id=student.id, match=match,
                content_type="chat_message", content_snippet="you are a badword",
            )
            await db.commit()

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/blacklist/triggers")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1
        item = body["items"][0]
        assert item["target_user_id"] == str(student.id)
        assert item["matched_pattern"] == "badword"
        assert item["content_type"] == "chat_message"

    async def test_moderator_cannot_see_the_raw_pattern_list_itself(self, db_session_factory, client_factory):
        """Confirms the deliberate tier split: a Moderator can see
        trigger EVENTS but not the underlying pattern LIST (which stays
        Owner-only, per `admin_blacklist.py`'s own module docstring)."""
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/blacklist/patterns")
        assert response.status_code == 403


# ---------------------------------------------------------------------
# Real blocking behavior -- profile bio (PATCH /users/me).
# ---------------------------------------------------------------------


class TestProfileBioBlocking:
    async def test_blacklisted_bio_is_rejected(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
        client = client_factory(student)
        response = client.patch("/api/v1/users/me", json={"bio": "I am a badword and proud of it"})
        assert response.status_code == 422
        assert "badword" not in response.json()["detail"]

        async with db_session_factory() as db:
            row = (await db.execute(text("SELECT action, outcome FROM moderation_log_entries"))).first()
            assert row is not None
            assert row[0] == "blacklist_triggered"
            assert json.loads(row[1])["content_type"] == "profile_bio"

    async def test_clean_bio_is_saved(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
        client = client_factory(student)
        response = client.patch("/api/v1/users/me", json={"bio": "I love math and chess"})
        assert response.status_code == 200
        assert response.json()["bio"] == "I love math and chess"

    async def test_updating_an_unrelated_field_does_not_trigger_a_check(self, db_session_factory, client_factory):
        """`exclude_unset` means `bio` is only checked when the caller
        actually sent it -- a PATCH touching only `major` must not run
        the (unrelated) bio check at all."""
        async with db_session_factory() as db:
            student = await _make_user(db)
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
        client = client_factory(student)
        response = client.patch("/api/v1/users/me", json={"major": "Physics"})
        assert response.status_code == 200
        assert response.json()["major"] == "Physics"


# ---------------------------------------------------------------------
# Real blocking behavior -- guild posts (service layer directly; the
# Gemini-backed `_moderate_content` call is monkeypatched since no real
# API key is available in this sandbox -- same posture as every other
# Gemini-backed test in this codebase. The blacklist check itself runs
# BEFORE that call, so this proves it short-circuits correctly.)
# ---------------------------------------------------------------------


class TestGuildPostBlocking:
    async def test_blacklisted_post_is_rejected_before_the_gemini_call(self, db_session_factory, monkeypatch):
        calls = {"count": 0}

        async def _fake_moderate(content):
            calls["count"] += 1
            raise AssertionError("Gemini moderation must not be called when the blacklist already rejected the content")

        monkeypatch.setattr(guild_service, "_moderate_content", _fake_moderate)

        async with db_session_factory() as db:
            author = await _make_user(db)
            guild = Guild(name="Test Guild", topic="testing", created_by=author.id)
            db.add(guild)
            db.add(BlacklistedPattern(pattern="badword"))
            await db.commit()
            await db.refresh(guild)

            with pytest.raises(guild_service.ContentRejectedError) as exc_info:
                await guild_service.create_post(db, author.id, guild.id, "this is a badword post", is_anonymous=False)
            assert "badword" not in str(exc_info.value)
            assert calls["count"] == 0

        async with db_session_factory() as db:
            posts = (await db.execute(text("SELECT COUNT(*) FROM guild_posts"))).scalar_one()
            assert posts == 0, "a rejected post must never be persisted"
            row = (await db.execute(text("SELECT action, outcome FROM moderation_log_entries"))).first()
            assert row is not None
            assert row[0] == "blacklist_triggered"
            assert json.loads(row[1])["content_type"] == "guild_post"

    async def test_clean_post_still_reaches_the_gemini_moderation_call(self, db_session_factory, monkeypatch):
        from app.services.guild_service import _ModerationResult

        async def _fake_moderate(content):
            return _ModerationResult(is_safe=True, rejection_reason=None)

        monkeypatch.setattr(guild_service, "_moderate_content", _fake_moderate)
        # is_anonymous=False below means `_generate_anonymous_alias` is
        # never called, so no mock is needed for it in this test.

        async with db_session_factory() as db:
            author = await _make_user(db)
            guild = Guild(name="Test Guild", topic="testing", created_by=author.id)
            db.add(guild)
            db.add(BlacklistedPattern(pattern="unrelatedword"))
            await db.commit()
            await db.refresh(guild)

            post = await guild_service.create_post(db, author.id, guild.id, "a perfectly clean post", is_anonymous=False)
            assert post.content == "a perfectly clean post"
