"""Behavioral tests for the Admin Override Dashboard & AI Decision
Correction System step: `app/api/v1/endpoints/admin_moderation.py`'s two
`RequireAdmin`-gated routes.

Real HTTP layer, via `TestClient` + `get_current_user`/`get_db`
dependency overrides -- same technique as `tests/test_courses_enrollment.py`'s
`TestEnrollRouterHTTP`, specifically because this step's own verification
requirement is about the REAL status code crossing the wire (a strict
`403` for a non-admin), not just about `require_admin`'s internal Python
behavior in isolation.

In-memory SQLite, `StaticPool`. Three tables needed:
- `users` -- raw SQL, same `_CREATE_USERS_TABLE_SQL` shape as
  `test_courses_enrollment.py` (SQLite has no `visit_ARRAY` for
  `User.skills`), PLUS `is_superuser` is exercised for real here (that
  other file never varied it) -- this suite's entire premise rests on
  toggling it between `True`/`False` per test user.
- `user_bookmarks` -- raw SQL, empty/unused, present ONLY because
  `User.bookmarks` is a `lazy="selectin"` relationship that eagerly
  queries this table the moment any `User` row is fetched/refreshed --
  same disclosed reason `test_courses_enrollment.py` needs it.
- `student_videos` -- raw SQL, reusing `tests/test_student_videos.py`'s
  own `_CREATE_STUDENT_VIDEOS_TABLE_SQL` column list VERBATIM plus the
  two new `admin_override`/`admin_notes` columns this step's own
  migration 0017 added -- same "JSONB has no SQLite DDL compiler
  support" reasoning already established there.
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
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
from app.models.user import User

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------
# Shared DB fixture
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
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# Empty, otherwise-unused -- see module docstring (`User.bookmarks`'s
# `lazy="selectin"` relationship).
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

# Verbatim column list from `tests/test_student_videos.py`'s own
# `_CREATE_STUDENT_VIDEOS_TABLE_SQL`, plus this step's own two new
# columns (`admin_override`/`admin_notes`, migration 0017) appended at
# the end -- kept in sync deliberately, per that file's own disclosed
# lesson about what happens when a hand-written raw-SQL test table
# silently drifts from the real model after a migration.
#
# `hls_master_url`/`thumbnail_url` RENAMED from `hls_master_cid`/
# `thumbnail_cid` (Student Video Pinata -> Cloudflare R2 Migration,
# migration 0018), widened to `VARCHAR(500)` to match the real model --
# same continuing discipline, updated in the SAME pass as the real
# column rename. Neither column is ever explicitly set by this file's
# own `_make_video` helper (it only supplies `ai_feedback`/`status`/
# etc. overrides), so the rename here is purely for schema-shape
# fidelity, not because any assertion in this file reads either field's
# value directly.
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
    is_read BOOLEAN NOT NULL DEFAULT 0,
    payload TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# NEW as of the Automatic Moderation Activity Log step: this file's own
# `override_video_decision`/`delete_video_permanently`/
# `reject_video_and_purge_media` calls now each write a real
# `ModerationLogEntry` row in the SAME transaction as their own
# mutation. Same "JSONB has no SQLite DDL compiler" reason as
# `notifications.payload` above -- raw SQL, `outcome` as plain `TEXT`.
_CREATE_MODERATION_LOG_TABLE_SQL = """
CREATE TABLE moderation_log_entries (
    id VARCHAR(36) PRIMARY KEY,
    actor_id VARCHAR(36),
    actor_role VARCHAR(20) NOT NULL,
    action VARCHAR(30) NOT NULL,
    target_type VARCHAR(20) NOT NULL,
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
        # Smart Notifications Engine addition: `override_video_decision`'s
        # own APPROVED-transition branch now calls BOTH `gamification_
        # service.add_xp` (added in the earlier Gamification milestone)
        # AND `notification_service.create_notification` (this step) --
        # neither of those tables previously existed in this file's own
        # fixture.
        #
        # DISCLOSED, NECESSARY FIX (found running the full suite during
        # the Real Leaderboard System task -- unrelated to that task's own
        # changes, a pre-existing regression from an EARLIER migration):
        # the comment that used to be here claimed "none of the three new
        # models have a JSONB/ARRAY column, so the ORM's own real DDL
        # compiles fine on SQLite unmodified" -- true when written, no
        # longer true since migration 0033 added a real `JSONB` column,
        # `Notification.payload` (Actionable Notifications), which
        # SQLite's DDL compiler cannot render (`CompileError`). Same fix
        # as `_CREATE_USERS_TABLE_SQL` above already uses for `User.skills`
        # (a Postgres `ARRAY`): a raw-SQL `CREATE TABLE`, `payload` as
        # plain `TEXT` -- only DDL compilation of the `JSONB` type itself
        # failed; ORM-level reads/writes of the `dict | None` Python value
        # still round-trip fine against a TEXT column.
        await conn.execute(text(_CREATE_NOTIFICATIONS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                GamificationProfile.__table__,
                Badge.__table__,
                UserBadge.__table__,
            ],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, is_superuser: bool, full_name: str = "Test User") -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        is_superuser=is_superuser,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str, **overrides) -> StudentVideo:
    video_id = overrides.pop("id", uuid.uuid4())
    defaults = dict(
        id=video_id,
        student_id=student_id,
        title="How Photosynthesis Works",
        description="A short explainer I made for biology class.",
        status=status,
        ai_safety_flag=False,
    )
    defaults.update(overrides)
    video = StudentVideo(**defaults)
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


@pytest.fixture
def client_factory(db_session_factory):
    """Returns a `(TestClient, user)` pair with `get_current_user`/`get_db`
    overridden to use the in-memory DB and a fixed, caller-chosen user --
    same technique as `test_courses_enrollment.py`'s own `client_factory`,
    generalized to take an already-built `User` instead of building one
    internally, since these tests need to build the ADMIN and the VIDEO's
    OWNER as two distinct users.
    """

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
# Tests
# ---------------------------------------------------------------------


class TestAdminGating:
    """This step's own explicit verification requirement #1: "a standard
    student receives a 403 Forbidden" on the admin endpoints.

    DISCLOSED, DELIBERATE MESSAGE CHANGE (Real Tiered Role System step):
    both routes below moved from `RequireAdmin` ("Only administrators can
    perform this action.") to `RequireModerationAccess` ("Requires
    moderator access or higher.") -- see `app/api/deps.py`'s
    `_require_admin_rank` and `AdminRole`'s own docstring
    (app/models/enums.py). The 403 itself is unchanged and still fully
    covered by these tests; only the detail string changed, updated here
    to match.
    """

    async def test_standard_student_gets_403_on_review_queue(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)

        client = client_factory(student)
        response = client.get("/api/v1/admin/videos/review-queue")

        assert response.status_code == 403
        assert response.json()["detail"] == "Requires moderator access or higher."

    async def test_standard_student_gets_403_on_override(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session, is_superuser=False)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(student)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "approved", "notes": "Looks fine to me."},
        )

        assert response.status_code == 403
        assert response.json()["detail"] == "Requires moderator access or higher."

        # Defense in depth: confirm the 403 genuinely blocked the write,
        # not just the response envelope -- the video must be completely
        # untouched in the DB.
        async with db_session_factory() as session:
            unchanged = await session.get(StudentVideo, video.id)
            assert unchanged.status == StudentVideoStatus.REJECTED.value
            assert unchanged.admin_override is False
            assert unchanged.admin_notes is None


class TestAdminOverride:
    """This step's own explicit verification requirement #2: "a verified
    Admin can successfully override a REJECTED video to APPROVED, with
    admin_override flipping to True in the database.\""""

    async def test_admin_overrides_rejected_to_approved(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            admin = await _make_user(session, is_superuser=True, full_name="Admin Person")
            student = await _make_user(session, is_superuser=False, full_name="Uploading Student")
            video = await _make_video(
                session,
                student_id=student.id,
                status=StudentVideoStatus.REJECTED.value,
                ai_feedback={"summary": "Bad audio.", "strengths": ["A", "B"], "growth_points": ["C", "D"], "tags": ["x", "y", "z"]},
            )

        client = client_factory(admin)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "approved", "notes": "Manually reviewed; audio issue was a false flag."},
        )

        assert response.status_code == 200
        body = response.json()
        assert body["success"] is True
        assert body["video_id"] == str(video.id)
        assert body["new_status"] == "approved"

        # The crux of this step's own verification requirement: re-query
        # the DB fresh (not the same in-request object) and confirm the
        # write actually persisted.
        async with db_session_factory() as session:
            persisted = await session.get(StudentVideo, video.id)
            assert persisted.status == StudentVideoStatus.APPROVED.value
            assert persisted.admin_override is True
            assert persisted.admin_notes == "Manually reviewed; audio issue was a false flag."

    async def test_admin_can_confirm_rejection(self, db_session_factory, client_factory):
        """The endpoint's other valid target -- an admin agreeing with
        Gemini's own REJECTED verdict, not just overturning it."""
        async with db_session_factory() as session:
            admin = await _make_user(session, is_superuser=True)
            student = await _make_user(session, is_superuser=False)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.REJECTED.value)

        client = client_factory(admin)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "rejected"},
        )

        assert response.status_code == 200
        assert response.json()["new_status"] == "rejected"

        async with db_session_factory() as session:
            persisted = await session.get(StudentVideo, video.id)
            assert persisted.status == StudentVideoStatus.REJECTED.value
            # Still flips to True even though the final status didn't
            # change -- an admin CONFIRMED the decision, which is itself
            # a reviewed/audited action per this step's own model.
            assert persisted.admin_override is True
            assert persisted.admin_notes is None

    async def test_invalid_target_status_rejected_with_422(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            admin = await _make_user(session, is_superuser=True)
            student = await _make_user(session, is_superuser=False)
            video = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(admin)
        response = client.post(
            f"/api/v1/admin/videos/{video.id}/override",
            json={"target_status": "pending_ai"},
        )

        assert response.status_code == 422

    async def test_override_on_nonexistent_video_returns_404(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            admin = await _make_user(session, is_superuser=True)

        client = client_factory(admin)
        response = client.post(
            f"/api/v1/admin/videos/{uuid.uuid4()}/override",
            json={"target_status": "approved"},
        )

        assert response.status_code == 404


class TestReviewQueue:
    """Filtering/sorting/shape checks for `GET /admin/videos/review-queue`."""

    async def test_review_queue_returns_only_flagged_statuses_oldest_first(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            admin = await _make_user(session, is_superuser=True)
            student = await _make_user(session, is_superuser=False, full_name="Jamie Uploader")

            # APPROVED is deliberately excluded from the queue -- inserted
            # here specifically to prove it does NOT show up below.
            await _make_video(session, student_id=student.id, status=StudentVideoStatus.APPROVED.value)

            rejected = await _make_video(
                session,
                student_id=student.id,
                status=StudentVideoStatus.REJECTED.value,
                ai_feedback={"summary": "s", "strengths": ["a", "b"], "growth_points": ["c", "d"], "tags": ["1", "2", "3"]},
            )
            failed = await _make_video(
                session,
                student_id=student.id,
                status=StudentVideoStatus.FAILED.value,
                ai_feedback={"error": "Whisper API is down"},
            )
            pending = await _make_video(session, student_id=student.id, status=StudentVideoStatus.PENDING_AI.value)

        client = client_factory(admin)
        response = client.get("/api/v1/admin/videos/review-queue")

        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 3

        ids_in_order = [item["id"] for item in body["items"]]
        # Oldest first -- all three were inserted in this same order
        # (rejected, failed, pending), so ascending `created_at` should
        # reproduce that exact insertion order.
        assert ids_in_order == [str(rejected.id), str(failed.id), str(pending.id)]

        rejected_item = body["items"][0]
        assert rejected_item["student_name"] == "Jamie Uploader"
        assert rejected_item["ai_feedback"]["summary"] == "s"
        assert rejected_item["pipeline_error"] is None

        failed_item = body["items"][1]
        assert failed_item["ai_feedback"] is None
        assert failed_item["pipeline_error"] == "Whisper API is down"

        pending_item = body["items"][2]
        assert pending_item["ai_feedback"] is None
        assert pending_item["pipeline_error"] is None
