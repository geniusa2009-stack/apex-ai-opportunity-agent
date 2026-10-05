"""Final Hardening Gate, Section 8: real concurrency testing of
`privacy_service.get_or_create_settings` and the settings-update path
(`PATCH /me/privacy-settings`), run as genuinely interleaved `asyncio`
tasks via `asyncio.gather` against a temp-file-based SQLite database
(see `tests/test_social_concurrency.py`'s module docstring for why a
real per-session-connection engine, not the rest of this suite's
`StaticPool` in-memory convention, is the correct tool for testing
actual concurrent transactions).

Covers the four scenarios Section 8 names: two concurrent settings-
creation attempts for the same user, two concurrent updates to the
same settings row, a concurrent update+read, and a concurrent
update+search (via `social_service`/`privacy_service`'s own building
blocks, not a full HTTP round-trip -- the underlying race is a DB-level
one, not a router-level one).
"""

from __future__ import annotations

import asyncio
import os
import tempfile
import uuid

import pytest
import pytest_asyncio
from sqlalchemy import select, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app.models.user import User
from app.models.user_privacy import UserPrivacySettings
from app.services import privacy_service

_CREATE_USERS_SQL = """
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
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT,
    interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT,
    remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_PRIVACY_SETTINGS_SQL = """
CREATE TABLE user_privacy_settings (
    user_id VARCHAR(36) PRIMARY KEY,
    bio_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    education_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    skills_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    location_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    languages_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# `User.bookmarks` is `lazy="selectin"` -- see `test_privacy.py`'s own
# fixture comment. Not actually exercised by any test here (no test
# loads a `User` row through the ORM), but created defensively so a
# future test added to this file doesn't silently hit "no such table".
_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36),
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def engine():
    """Real temp-file SQLite -- genuinely separate per-session
    connections, not `StaticPool`-shared. See the module docstring."""
    fd, path = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    os.remove(path)
    eng = create_async_engine(f"sqlite+aiosqlite:///{path}", connect_args={"timeout": 5})
    async with eng.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_USER_PRIVACY_SETTINGS_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))
    yield eng
    await eng.dispose()
    if os.path.exists(path):
        os.remove(path)


async def _new_session(engine) -> AsyncSession:
    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    return session_factory()


async def _make_and_commit_user(engine) -> uuid.UUID:
    user_id = uuid.uuid4()
    session = await _new_session(engine)
    session.add(User(id=user_id, email=f"{uuid.uuid4().hex}@example.com", full_name="User"))
    await session.commit()
    await session.close()
    return user_id


class TestPrivacySettingsConcurrency:
    @pytest.mark.asyncio
    async def test_two_concurrent_creation_attempts_produce_exactly_one_row(self, engine):
        """Two concurrent `get_or_create_settings` calls for a user who
        has never opened Privacy & Visibility before (e.g. two tabs
        both loading `GET /me/privacy-settings` at once). Expected: no
        unhandled `IntegrityError`, exactly one row ever exists, and
        every concurrent caller gets back a usable, all-"public" view
        -- `get_or_create_settings`'s own SAVEPOINT + `IntegrityError`
        race guard (Phase 1) is what's under test here, now actually
        exercised under real concurrency rather than only read from."""
        user_id = await _make_and_commit_user(engine)

        async def create():
            session = await _new_session(engine)
            settings = await privacy_service.get_or_create_settings(session, user_id)
            await session.commit()
            await session.refresh(settings)
            result = settings.bio_visibility
            await session.close()
            return result

        results = await asyncio.gather(create(), create(), return_exceptions=True)

        failed = [r for r in results if isinstance(r, Exception)]
        assert failed == []  # no unhandled IntegrityError ever surfaces to the caller
        assert results == ["public", "public"]  # both callers see a consistent, usable default

        verify = await _new_session(engine)
        rows = (await verify.execute(
            select(UserPrivacySettings).where(UserPrivacySettings.user_id == user_id)
      )).scalars().all()
        assert len(rows) == 1  # never a duplicate settings row
        await verify.close()

    @pytest.mark.asyncio
    async def test_two_concurrent_updates_to_different_fields_both_persist(self, engine):
        """Two concurrent updates to DIFFERENT categories on the same
        settings row (e.g. one tab changing bio visibility while
        another changes skills visibility). Expected: no partial/lost
        update -- both changes end up persisted, not one silently
        overwritten by the other's stale snapshot."""
        user_id = await _make_and_commit_user(engine)
        setup = await _new_session(engine)
        await privacy_service.get_or_create_settings(setup, user_id)
        await setup.commit()
        await setup.close()

        async def update(field, value):
            session = await _new_session(engine)
            settings = await privacy_service.get_or_create_settings(session, user_id)
            setattr(settings, field, value)
            session.add(settings)
            await session.commit()
            await session.close()

        results = await asyncio.gather(
            update("bio_visibility", "connections"),
            update("skills_visibility", "private"),
            return_exceptions=True,
        )
        assert [r for r in results if isinstance(r, Exception)] == []

        verify = await _new_session(engine)
        final = await verify.get(UserPrivacySettings, user_id)
        assert final.bio_visibility == "connections"
        assert final.skills_visibility == "private"
        # Untouched-by-either categories keep their defalt -- neither
        # concurrent writer's stale snapshot clobbered anything it
        # didn't itself set.
        assert final.education_visibility == "public"
        await verify.close()

    @pytest.mark.asyncio
    async def test_two_concurrent_updates_to_the_same_field_yield_one_valid_value(self, engine):
        """Two concurrent updates to the SAME category (e.g. two tabs
        both flipping bio visibility to different values at once).
        Expected: a valid, DB-independent last-write-wins outcome --
        the final value is one of the two written values, never a
        third, corrupted one, and no unhandled integrity error."""
        user_id = await _make_and_commit_user(engine)
        setup = await _new_session(engine)
        await privacy_service.get_or_create_settings(setup, user_id)
        await setup.commit()
        await setup.close()

        async def update(value):
            session = await _new_session(engine)
            settings = await privacy_service.get_or_create_settings(session, user_id)
            settings.bio_visibility = value
            session.add(settings)
            await session.commit()
            await session.close()

        results = await asyncio.gather(
            update("connections"), update("private"), return_exceptions=True
        )
        assert [r for r in results if isinstance(r, Exception)] == []

        verify = await _new_session(engine)
        final = await verify.get(UserPrivacySettings, user_id)
        assert final.bio_visibility in ("connections", "private")
        await verify.close()

    @pytest.mark.asyncio
    async def test_concurrent_update_and_read_never_raises_or_returns_a_half_written_row(self, engine):
        """An update racing a read of the same settings row (e.g. the
        user saves Privacy & Visibility while `GET /users/{id}/profile`
        is concurrently resolving someone else's view of them).
        Expected: the reader always gets a fully-formed, valid row --
        one of the two legitimate before/after states, never a crash
        and never a partially-applied value."""
        user_id = await _make_and_commit_user(engine)
        setup = await _new_session(engine)
        await privacy_service.get_or_create_settings(setup, user_id)
        await setup.commit()
        await setup.close()

        async def update():
            session = await _new_session(engine)
            settings = await privacy_service.get_or_create_settings(session, user_id)
            settings.bio_visibility = "private"
            session.add(settings)
            await session.commit()
            await session.close()

        async def read():
            session = await _new_session(engine)
            settings = await privacy_service.get_or_create_settings(session, user_id)
            result = settings.bio_visibility
            await session.close()
            return result

        results = await asyncio.gather(update(), read(), return_exceptions=True)
        failed = [r for r in results if isinstance(r, Exception)]
        assert failed == []
        read_result = results[1]
        assert read_result in ("public", "private")  # the reader never sees a torn/invalid value

    @pytest.mark.asyncio
    async def test_concurrent_update_and_bulk_settings_lookup_never_raises(self, engine):
        """An update racing the bulk settings lookup `GET /users/search`
        performs for a whole page of results (`privacy_service.
        UserPrivacySettings.user_id.in_([...])`, see `users.py`'s
        search handler) -- exercises the exact same bulk-fetch query
        this phase's own N+1 fix relies on, under real concurrent
        write pressure. Expected: no exception, and the bulk lookup
        always returns a fully-formed, valid row for the target."""
        user_id = await _make_and_commit_user(engine)
        setup = await _new_session(engine)
        await privacy_service.get_or_create_settings(setup, user_id)
        await setup.commit()
        await setup.close()

        async def update():
            session = await _new_session(engine)
            settings = await privacy_service.get_or_create_settings(session, user_id)
            settings.location_visibility = "connections"
            session.add(settings)
            await session.commit()
            await session.close()

        async def bulk_lookup():
            session = await _new_session(engine)
            result = await session.execute(
                select(UserPrivacySettings).where(UserPrivacySettings.user_id.in_([user_id]))
            )
            rows = result.scalars().all()
            await session.close()
            return [row.location_visibility for row in rows]

        results = await asyncio.gather(update(), bulk_lookup(), return_exceptions=True)
        failed = [r for r in results if isinstance(r, Exception)]
        assert failed == []
        lookup_result = results[1]
        assert len(lookup_result) == 1  # the row the search page is scanning is never missing/duplicated
        assert lookup_result[0] in ("public", "connections")
