"""Behavioral tests for Phase 5 (Contribution Stats + The 3 Views,
backend-only): `app/services/moderation_stats_service.py`'s real
aggregation queries (`period_bounds`, `get_team_summary`,
`get_today_digest`) and the two real HTTP endpoints in
`app/api/v1/endpoints/admin_stats.py` (`GET /admin/team/summary`,
`GET /admin/today`).

DISCLOSED, NECESSARY DEVIATION from `tests/test_moderation_claims.py`'s
own `from app.main import app` pattern: this sandbox's `pip install`
against the full `requirements.txt` reproducibly could not complete in
this session -- `google-genai`'s own resolution/download stalled
indefinitely in isolation (confirmed by installing every other package
in `requirements.txt` successfully in ~11s total, then reproducing the
identical hang with `google-genai` alone, repeatedly, across several
independent attempts), and once that was worked around (installing it
with `--no-deps` first, then its real transitive deps individually),
attempting to install `supabase`/`boto3`/`celery`/`flower` afterward hit
a SEPARATE, also-reproducible failure: this sandbox's mounted
`outputs` filesystem returns `OSError: [Errno 1] Operation not
permitted` on any attempt to overwrite/replace a FILE THAT ALREADY
EXISTS (confirmed with a plain `rm` on an unrelated log file, before any
of this session's own package installs) -- which is exactly what `pip`
needs to do to upgrade an already-installed package's file (e.g.
`websockets`, pulled in by both `google-genai` and `supabase` at
different version floors). This is a NEW, more severe sandbx
environment limitation than the previously-disclosed PGlite/asyncpg
one from Phases 3-4 (that one blocked only the LIVE HTTP proof step,
after real unit tests already passed cleanly; this one blocks
installing the dependencies `app.main` itself needs to import at all,
in THIS particular fresh sandbox session).

Real, disclosed workaround: this suite builds its own minimal `FastAPI`
app containing ONLY `admin_stats_router` (`app.api.v1.endpoints.
admin_stats.router`) -- real routing, real `RequireOwner` dependency,
real request/response schemas, real DB queries -- instead of importing
the full `app.main.app`, which transitively imports all 90+ of this
codebase's endpoint modules, including several this session could not
finish installing. `admin_stats.py` and everything it imports
(`app.api.deps`, `app.services.moderation_stats_service`,
`app.services.moderation_claim_service`, the `StudentVideo`/
`ModerationLogEntry`/`User` models) import and run cleanly in this
environment -- confirmed directly, not assumed -- since none of them
touch `supabase`/`boto3`/`celery`/`scrapy`/`langchain`/`playwright` at
all. The service-layer tests below (`TestPeriodBounds`,
`TestGetTeamSummary`, `TestGetTodayDigest`) call
`moderation_stats_service` functions directly against a real seeded
SQLite session with no HTTP layer at all -- these are the tests that
actually prove the real aggregation queries and their real output
against known data, per this step's own Step 4 ask. The HTTP-level
tests (`TestTeamSummaryEndpoint`, `TestTodayEndpoint`) layer real
routing/auth/permission checks on top, via the scoped-down app above.
"""

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.endpoints.admin_stats import router as admin_stats_router
from app.db.base import Base
from app.db.session import get_db
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType, StudentVideoStatus
from app.models.moderation_log import ModerationLogEntry
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import moderation_stats_service
from app.services.moderation_claim_service import CLAIM_TIMEOUT_MINUTES

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

# Needed the same real reason as `tests/test_moderation_claims.py`'s own
# fixture: `User` has a real ORM relationship to `user_bookmarks`, lazily
# loaded on `db.refresh(user)` -- omitting this table produces a real
# `OperationalError: no such table: user_bookmarks` the first time any
# test here refreshes a seeded `User` row.
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

# Same real column list (including `claimed_by`/`claimed_at`, migration
# 0039) as `tests/test_moderation_claims.py`'s own fixture -- this
# suite's `TestGetTodayDigest` cases rest on them the same way that
# suite's do.
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
    actor_role VARCHAR(20) NOT NULL,
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(20) NOT NULL,
    target_id VARCHAR(36) NOT NULL,
    outcome TEXT NOT NULL,
    created_at DATETIME NOT NULL
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

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession, *, admin_role: AdminRole | None = None, is_superuser: bool = False, full_name: str = "Test User"
) -> User:
    user = User(
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name,
        is_superuser=is_superuser, admin_role=admin_role,
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


async def _log(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID | None,
    action: ModerationAction,
    target_type: ModerationTargetType,
    target_id: uuid.UUID,
    created_at: dt.datetime,
    actor_role: AdminRole = AdminRole.MODERATOR,
) -> ModerationLogEntry:
    """Direct raw-SQL insert (not the ORM `log_action` helper) -- this
    suite needs full, explicit control over `created_at` (to place
    entries precisely at period boundaries and inside/outside "today"),
    which `log_action`'s real `server_default=func.now()` posture
    deliberately doesn't allow overriding via the ORM's normal
    `db.add()` + `db.commit()` path against SQLite (unlike Postgres,
    SQLite's `server_default=CURRENT_TIMESTAMP` still fires even when an
    explicit value is set, in this codebase's tested configuration) --
    a plain `INSERT` sidesteps that entirely, same posture as every
    other raw-SQL seed helper in this codebase's own test suite.
    """
    # `.hex` (32-char, no dashes), NOT `str()` (36-char, dashed) --
    # matches the app's own `UUID(as_uuid=True)` column type's real
    # on-disk SQLite representation. This is the identical fix already
    # applied at the end of Phase 3 (disclosed there): a raw-SQL insert
    # using `str(some_uuid)` writes a dashed string that never equality-
    # matches a `UUID(as_uuid=True)` column's own `.hex`-normalized
    # values, silently breaking every join/lookup against it without
    # ever raising an error -- exactly the failure mode this helper
    # must not reintroduce.
    entry_id = uuid.uuid4()
    await db.execute(
        text(
            """INSERT INTO moderation_log_entries
            (id, actor_id, actor_role, action, target_type, target_id, outcome, created_at)
            VALUES (:id, :actor_id, :actor_role, :action, :target_type, :target_id, :outcome, :created_at)"""
        ),
        {
            "id": entry_id.hex,
            "actor_id": actor_id.hex if actor_id else None,
            "actor_role": actor_role.value,
            "action": action.value,
            "target_type": target_type.value,
            "target_id": target_id.hex,
            "outcome": "{}",
            "created_at": created_at,
        },
    )
    await db.commit()
    return entry_id


# ---------------------------------------------------------------------
# Step 1 / Step 2: `period_bounds` -- real week/month boundary math.
# ---------------------------------------------------------------------


class TestPeriodBounds:
    def test_week_starts_monday_and_ends_next_monday(self):
        # 2026-03-11 is a Wednesday.
        now = dt.datetime(2026, 3, 11, 15, 30, tzinfo=dt.timezone.utc)
        start, end = moderation_stats_service.period_bounds("week", now=now)
        assert start == dt.datetime(2026, 3, 9, 0, 0, tzinfo=dt.timezone.utc)  # Monday
        assert end == dt.datetime(2026, 3, 16, 0, 0, tzinfo=dt.timezone.utc)  # next Monday
        assert (end - start) == dt.timedelta(days=7)

    def test_month_is_calendar_aligned(self):
        now = dt.datetime(2026, 2, 17, 8, 0, tzinfo=dt.timezone.utc)
        start, end = moderation_stats_service.period_bounds("month", now=now)
        assert start == dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc)
        assert end == dt.datetime(2026, 3, 1, tzinfo=dt.timezone.utc)

    def test_month_rolls_over_december_correctly(self):
        now = dt.datetime(2026, 12, 25, tzinfo=dt.timezone.utc)
        start, end = moderation_stats_service.period_bounds("month", now=now)
        assert start == dt.datetime(2026, 12, 1, tzinfo=dt.timezone.utc)
        assert end == dt.datetime(2027, 1, 1, tzinfo=dt.timezone.utc)

    def test_unknown_period_raises(self):
        with pytest.raises(ValueError):
            moderation_stats_service.period_bounds("year")


# ---------------------------------------------------------------------
# Step 2: `get_team_summary` -- real GROUP BY aggregation against known
# seeded data, including the real response-time-eligibility rule.
# ---------------------------------------------------------------------


class TestGetTeamSummary:
    pytestmark = pytest.mark.asyncio

    async def test_counts_and_response_time_against_known_data(self, db_session_factory):
        window_start = dt.datetime(2026, 3, 9, tzinfo=dt.timezone.utc)
        window_end = dt.datetime(2026, 3, 16, tzinfo=dt.timezone.utc)

        async with db_session_factory() as db:
            mod_a = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod A")
            mod_b = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Mod B")
            student = await _make_user(db)

            # mod_a: two response-time-eligible actions on two videos,
            # each with a KNOWN, exact gap -- 3600s and 7200s, so the
            # real expected average is exactly 5400.0s.
            v1 = await _make_video(
                db, student_id=student.id, status=StudentVideoStatus.REJECTED.value,
                created_at=dt.datetime(2026, 3, 10, 8, 0, tzinfo=dt.timezone.utc),
            )
            await _log(
                db, actor_id=mod_a.id, action=ModerationAction.VIDEO_OVERRIDE,
                target_type=ModerationTargetType.STUDENT_VIDEO, target_id=v1.id,
                created_at=dt.datetime(2026, 3, 10, 9, 0, tzinfo=dt.timezone.utc),  # +3600s
            )
            v2 = await _make_video(
                db, student_id=student.id, status=StudentVideoStatus.REJECTED.value,
                created_at=dt.datetime(2026, 3, 11, 8, 0, tzinfo=dt.timezone.utc),
            )
            await _log(
                db, actor_id=mod_a.id, action=ModerationAction.VIDEO_REJECT_AND_PURGE,
                target_type=ModerationTargetType.STUDENT_VIDEO, target_id=v2.id,
                created_at=dt.datetime(2026, 3, 11, 10, 0, tzinfo=dt.timezone.utc),  # +7200s
            )

            # mod_b: one VIDEO_DELETE whose target row is genuinely gone
            # (never inserted here -- proving the response-time query's
            # INNER JOIN correctly drops it from the sample without
            # crashing) and one CHAT_UNBAN (targets a USER, never
            # eligible at all). Both must still count toward
            # total_actions/action_counts.
            deleted_video_id = uuid.uuid4()
            await _log(
                db, actor_id=mod_b.id, action=ModerationAction.VIDEO_DELETE,
                target_type=ModerationTargetType.STUDENT_VIDEO, target_id=deleted_video_id,
                created_at=dt.datetime(2026, 3, 12, 8, 0, tzinfo=dt.timezone.utc),
            )
            await _log(
                db, actor_id=mod_b.id, action=ModerationAction.CHAT_UNBAN,
                target_type=ModerationTargetType.USER, target_id=student.id,
                created_at=dt.datetime(2026, 3, 12, 9, 0, tzinfo=dt.timezone.utc),
            )

            # Outside the window entirely -- must not affect either
            # volunteer's totals.
            await _log(
                db, actor_id=mod_a.id, action=ModerationAction.VIDEO_OVERRIDE,
                target_type=ModerationTargetType.STUDENT_VIDEO, target_id=v1.id,
                created_at=dt.datetime(2026, 2, 1, tzinfo=dt.timezone.utc),
            )

            rows = await moderation_stats_service.get_team_summary(db, start=window_start, end=window_end)

        by_actor = {row.actor_id: row for row in rows}

        mod_a_row = by_actor[mod_a.id]
        assert mod_a_row.actor_full_name == "Mod A"
        assert mod_a_row.total_actions == 2
        assert mod_a_row.action_counts == {"video_override": 1, "video_reject_and_purge": 1}
        assert mod_a_row.response_time_sample_count == 2
        assert mod_a_row.avg_response_time_seconds == pytest.approx(5400.0)

        mod_b_row = by_actor[mod_b.id]
        assert mod_b_row.total_actions == 2
        assert mod_b_row.action_counts == {"video_delete": 1, "chat_unban": 1}
        assert mod_b_row.response_time_sample_count == 0
        assert mod_b_row.avg_response_time_seconds is None

    async def test_entry_exactly_at_period_start_is_included_exactly_at_end_is_excluded(self, db_session_factory):
        start = dt.datetime(2026, 3, 9, tzinfo=dt.timezone.utc)
        end = dt.datetime(2026, 3, 16, tzinfo=dt.timezone.utc)

        async with db_session_factory() as db:
            mod = await _make_user(db, admin_role=AdminRole.MODERATOR)
            student = await _make_user(db)
            v = await _make_video(db, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

            await _log(
                db, actor_id=mod.id, action=ModerationAction.CHAT_UNBAN,
                target_type=ModerationTargetType.USER, target_id=student.id, created_at=start,
            )
            await _log(
                db, actor_id=mod.id, action=ModerationAction.CHAT_UNBAN,
                target_type=ModerationTargetType.USER, target_id=student.id, created_at=end,
            )

            rows = await moderation_stats_service.get_team_summary(db, start=start, end=end)

        assert len(rows) == 1
        assert rows[0].total_actions == 1

    async def test_no_activity_in_window_returns_empty_list(self, db_session_factory):
        async with db_session_factory() as db:
            rows = await moderation_stats_service.get_team_summary(
                db,
                start=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                end=dt.datetime(2026, 1, 8, tzinfo=dt.timezone.utc),
            )
        assert rows == []


# ---------------------------------------------------------------------
# Step 3: `get_today_digest` -- real totals + real LIVE unclaimed-wait
# snapshot.
# ---------------------------------------------------------------------


class TestGetTodayDigest:
    pytestmark = pytest.mark.asyncio

    async def test_totals_and_longest_unclaimed_wait_against_known_data(self, db_session_factory):
        now = dt.datetime(2026, 3, 10, 18, 0, tzinfo=dt.timezone.utc)
        today_start = dt.datetime(2026, 3, 10, tzinfo=dt.timezone.utc)

        async with db_session_factory() as db:
            mod_a = await _make_user(db, admin_role=AdminRole.MODERATOR)
            mod_b = await _make_user(db, admin_role=AdminRole.MODERATOR)
            student = await _make_user(db)

            # Two actions today by two different volunteers -> total_actions=2, active=2.
            await _log(
                db, actor_id=mod_a.id, action=ModerationAction.CHAT_UNBAN,
                target_type=ModerationTargetType.USER, target_id=student.id,
                created_at=today_start + dt.timedelta(hours=1),
            )
            await _log(
                db, actor_id=mod_b.id, action=ModerationAction.CHAT_UNBAN,
                target_type=ModerationTargetType.USER, target_id=student.id,
                created_at=today_start + dt.timedelta(hours=2),
            )
            # Yesterday -- must not count.
            await _log(
                db, actor_id=mod_a.id, action=ModerationAction.CHAT_UNBAN,
                target_type=ModerationTargetType.USER, target_id=student.id,
                created_at=today_start - dt.timedelta(hours=1),
            )

            # video_expired_claim: OLDEST real `created_at` (5 hours
            # before `now`), claim is STALE (45min > 30min timeout) ->
            # must be treated as the real longest-waiting UNCLAIMED item.
            video_expired_claim = await _make_video(
                db, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value,
                created_at=now - dt.timedelta(hours=5),
                claimed_by=mod_a.id, claimed_at=now - dt.timedelta(minutes=CLAIM_TIMEOUT_MINUTES + 15),
            )
            # video_unclaimed_newer: unclaimed, but newer than the expired-claim video above.
            await _make_video(
                db, student_id=student.id, status=StudentVideoStatus.FAILED.value,
                created_at=now - dt.timedelta(hours=2), claimed_by=None,
            )
            # video_actively_claimed: OLDER than either of the above,
            # but its claim is still ACTIVE (5 min old) -- must be
            # excluded from the unclaimed-wait computation entirely.
            await _make_video(
                db, student_id=student.id, status=StudentVideoStatus.REJECTED.value,
                created_at=now - dt.timedelta(hours=10),
                claimed_by=mod_b.id, claimed_at=now - dt.timedelta(minutes=5),
            )
            # video_approved: OLDEST of all and unclaimed, but APPROVED
            # -- not a review-queue status at all, must be excluded.
            await _make_video(
                db, student_id=student.id, status=StudentVideoStatus.APPROVED.value,
                created_at=now - dt.timedelta(hours=100), claimed_by=None,
            )

            digest = await moderation_stats_service.get_today_digest(db, now=now)

        assert digest.date == dt.date(2026, 3, 10)
        assert digest.window_start == today_start
        assert digest.window_end == today_start + dt.timedelta(days=1)
        assert digest.total_actions == 2
        assert digest.active_volunteer_count == 2
        assert digest.longest_unclaimed_item_id == video_expired_claim.id
        assert digest.longest_unclaimed_wait_seconds == pytest.approx(5 * 3600)

    async def test_no_unclaimed_queue_items_returns_none_not_zero(self, db_session_factory):
        now = dt.datetime(2026, 3, 10, 12, 0, tzinfo=dt.timezone.utc)
        async with db_session_factory() as db:
            digest = await moderation_stats_service.get_today_digest(db, now=now)
        assert digest.total_actions == 0
        assert digest.active_volunteer_count == 0
        assert digest.longest_unclaimed_wait_seconds is None
        assert digest.longest_unclaimed_item_id is None


# ---------------------------------------------------------------------
# HTTP layer: real routing + real `RequireOwner` gating, via a scoped
# app containing only `admin_stats_router` (see this file's own module
# docstring for why not `app.main.app` in this sandbox session).
# ---------------------------------------------------------------------


def _build_scoped_app() -> FastAPI:
    scoped_app = FastAPI()
    scoped_app.include_router(admin_stats_router, prefix="/api/v1")
    return scoped_app


@pytest.fixture
def client_factory(db_session_factory):
    scoped_app = _build_scoped_app()

    def _build(user: User) -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        async def _override_get_current_user():
            async with db_session_factory() as session:
                return await session.get(User, user.id)

        scoped_app.dependency_overrides[get_db] = _override_get_db
        scoped_app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(scoped_app)

    yield _build
    scoped_app.dependency_overrides.clear()


class TestTeamSummaryEndpoint:
    pytestmark = pytest.mark.asyncio

    async def test_owner_gets_200_with_real_shape(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/team/summary?period=week")
        assert response.status_code == 200
        body = response.json()
        assert body["period"] == "week"
        assert "items" in body

    async def test_moderator_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/team/summary")
        assert response.status_code == 403

    async def test_invalid_period_422s(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/team/summary?period=year")
        assert response.status_code == 422


class TestTodayEndpoint:
    pytestmark = pytest.mark.asyncio

    async def test_owner_gets_200_with_real_shape(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/today")
        assert response.status_code == 200
        body = response.json()
        assert "total_actions" in body
        assert "active_volunteer_count" in body
        assert "longest_unclaimed_wait_seconds" in body

    async def test_moderator_gets_403(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/today")
        assert response.status_code == 403
