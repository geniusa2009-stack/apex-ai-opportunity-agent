"""Behavioral tests for the Video Analytics, Watch Time Tracking &
Engagement Engine step: `app/services/analytics_service.py` (the atomic
upsert + completion-threshold business logic) and
`app/api/v1/endpoints/analytics.py` (the real HTTP layer on top of it).

In-memory SQLite, `StaticPool`. Table creation strategy, per this
codebase's own established discipline (see `tests/test_student_videos.py`/
`tests/test_courses_enrollment.py`'s own docstrings on this exact point):

- `users` -- raw SQL (`User.skills` is a Postgres `ARRAY`, which
  SQLite's DDL compiler has no `visit_ARRAY` for).
- `user_bookmarks` -- raw SQL, empty/unused, present ONLY because
  `User.bookmarks` is a `lazy="selectin"` relationship that eagerly
  queries this table the moment any `User` row is fetched/refreshed.
- `student_videos` -- raw SQL, verbatim column list from
  `tests/test_student_videos.py`'s own `_CREATE_STUDENT_VIDEOS_TABLE_SQL`
  (`ai_feedback`/`transcript_segments` are `JSONB`, no SQLite DDL
  compiler support).
- `lessons` -- raw SQL, new to THIS file: `Lesson.video_metadata` is
  also `JSONB`, the identical category of dialect gap.
- `courses`, `course_modules`, `user_enrollments`, `lesson_progress`,
  `student_accountability`, `video_playback_logs` -- `Base.metadata.
  create_all(tables=[...])` directly. None of these six have a `JSONB`/
  `ARRAY` column (`Course.embedding` is a pgvector `Vector` column,
  already confirmed elsewhere in this codebase to compile fine on
  SQLite), so the ORM's own real DDL works unmodified.

Two test classes:
- `TestHeartbeatService` -- exercises `analytics_service.record_heartbeat`/
  `get_playback_progress` directly against a real DB session, covering
  this step's own two explicit verification requirements (successive
  heartbeats accumulate watch time mathematically; crossing the 95%
  threshold flips `is_completed`) plus the polymorphic UGC/COURSE
  branching, the best-effort completion-reward callback, and the
  resume-position read path.
- `TestHeartbeatRouterHTTP` -- exercises the real FastAPI routes via
  `TestClient`, with `get_current_user`/`get_db` dependency-overridden
  (same technique as every other router-level test in this codebase),
  to prove the real response envelope and status codes cross the wire.
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
from app.models.analytics import VideoPlaybackLog
from app.models.courses import Course, CourseModule, Lesson, LessonProgress, UserEnrollment
from app.models.enums import StudentVideoStatus, VideoType
from app.models.gamification import StudentAccountability
from app.models.student_videos import StudentVideo
from app.models.user import User
from app.services import analytics_service, course_service

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------
# Raw-SQL tables (JSONB/ARRAY columns SQLite can't natively DDL-compile)
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

# Verbatim from `tests/test_student_videos.py`'s own
# `_CREATE_STUDENT_VIDEOS_TABLE_SQL`, post R2-migration column names.
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

# New to this file -- `Lesson.video_metadata` is `JSONB`, same dialect
# gap as `student_videos.ai_feedback` above.
_CREATE_LESSONS_TABLE_SQL = """
CREATE TABLE lessons (
    id VARCHAR(36) PRIMARY KEY,
    module_id VARCHAR(36) NOT NULL,
    title VARCHAR(255) NOT NULL,
    content_type VARCHAR(50) NOT NULL,
    video_url VARCHAR(1000),
    text_content TEXT,
    order_index INTEGER NOT NULL DEFAULT 0,
    hls_master_cid VARCHAR(255),
    duration_seconds INTEGER,
    video_processing_status VARCHAR(50),
    video_metadata TEXT,
    thumbnail_cid VARCHAR(255)
)
"""


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_TABLE_SQL))
        await conn.execute(text(_CREATE_STUDENT_VIDEOS_TABLE_SQL))
        await conn.execute(text(_CREATE_LESSONS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                Course.__table__,
                CourseModule.__table__,
                UserEnrollment.__table__,
                LessonProgress.__table__,
                StudentAccountability.__table__,
                VideoPlaybackLog.__table__,
            ],
        )

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


@pytest_asyncio.fixture
async def db_session(db_session_factory):
    async with db_session_factory() as session:
        yield session


# ---------------------------------------------------------------------
# Fixture-data helpers
# ---------------------------------------------------------------------


async def _make_user(db: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test Student")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_student_video(db: AsyncSession, student_id: uuid.UUID, duration_seconds: int | None) -> StudentVideo:
    video = StudentVideo(
        id=uuid.uuid4(),
        student_id=student_id,
        title="A Presentation",
        status=StudentVideoStatus.APPROVED.value,
        duration_seconds=duration_seconds,
    )
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


async def _make_course_with_lesson(db: AsyncSession, duration_seconds: int | None) -> tuple[Course, Lesson]:
    course = Course(id=uuid.uuid4(), title="Intro to Testing", instructor_name="AI Scholar Guide", token_unlock_cost=0)
    db.add(course)
    await db.commit()
    await db.refresh(course)

    module = CourseModule(id=uuid.uuid4(), course_id=course.id, title="Module 1", order_index=0)
    db.add(module)
    await db.commit()
    await db.refresh(module)

    lesson = Lesson(
        id=uuid.uuid4(),
        module_id=module.id,
        title="Lesson 1",
        content_type="video",
        order_index=0,
        duration_seconds=duration_seconds,
        video_processing_status="completed",
    )
    db.add(lesson)
    await db.commit()
    await db.refresh(lesson)

    return course, lesson


async def _enroll(db: AsyncSession, user_id: uuid.UUID, course_id: uuid.UUID) -> None:
    db.add(UserEnrollment(user_id=user_id, course_id=course_id))
    await db.commit()


def _heartbeat(video_id: uuid.UUID, video_type: VideoType, current_time: float, delta: float, is_ended: bool = False):
    from app.schemas.analytics import HeartbeatRequest

    return HeartbeatRequest(
        video_id=video_id,
        video_type=video_type,
        current_time_seconds=current_time,
        delta_duration_seconds=delta,
        is_ended=is_ended,
    )


# ---------------------------------------------------------------------
# Service-layer tests
# ---------------------------------------------------------------------


class TestHeartbeatService:
    async def test_successive_heartbeats_accumulate_watch_time_correctly(self, db_session):
        """This step's own explicit verification requirement #1."""
        user = await _make_user(db_session)
        video = await _make_student_video(db_session, user.id, duration_seconds=100)

        r1 = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=10.0, delta=10.0)
        )
        assert r1.cumulative_watch_time_seconds == pytest.approx(10.0)
        assert r1.last_position_seconds == pytest.approx(10.0)

        r2 = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=20.0, delta=10.0)
        )
        assert r2.cumulative_watch_time_seconds == pytest.approx(20.0), "must ADD to the prior total, not replace it"
        assert r2.last_position_seconds == pytest.approx(20.0)

        r3 = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=30.0, delta=10.0)
        )
        assert r3.cumulative_watch_time_seconds == pytest.approx(30.0)
        assert r3.last_position_seconds == pytest.approx(30.0)
        assert r3.is_completed is False

        # Re-query fresh from the DB (not the same in-request object) --
        # confirms the accumulation genuinely persisted, not just the
        # in-memory response.
        result = await db_session.execute(
            VideoPlaybackLog.__table__.select().where(VideoPlaybackLog.user_id == user.id)
        )
        row = result.fetchone()
        assert row.cumulative_watch_time_seconds == pytest.approx(30.0)

    async def test_crossing_95_percent_threshold_flips_is_completed(self, db_session):
        """This step's own explicit verification requirement #2."""
        user = await _make_user(db_session)
        video = await _make_student_video(db_session, user.id, duration_seconds=100)

        below = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=50.0, delta=10.0)
        )
        assert below.is_completed is False, "50% watched must not be completed"

        crossing = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=96.0, delta=10.0)
        )
        assert crossing.is_completed is True, "96% (>= 95%) must flip completion automatically"

    async def test_is_ended_forces_completion_below_threshold(self, db_session):
        user = await _make_user(db_session)
        video = await _make_student_video(db_session, user.id, duration_seconds=100)

        result = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=10.0, delta=10.0, is_ended=True)
        )
        assert result.is_completed is True, "is_ended=True must complete the video regardless of position"

    async def test_unknown_duration_never_auto_completes_without_is_ended(self, db_session):
        """A video whose pipeline hasn't resolved a duration yet
        (`duration_seconds IS NULL`) has no denominator to compute a
        percentage against -- must never silently mark itself complete
        just because `current_time_seconds` is numerically large."""
        user = await _make_user(db_session)
        video = await _make_student_video(db_session, user.id, duration_seconds=None)

        result = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=9999.0, delta=10.0)
        )
        assert result.is_completed is False

    async def test_heartbeat_for_nonexistent_video_raises(self, db_session):
        user = await _make_user(db_session)

        with pytest.raises(analytics_service.VideoNotFoundError):
            await analytics_service.record_heartbeat(
                db_session, user.id, _heartbeat(uuid.uuid4(), VideoType.UGC, current_time=1.0, delta=1.0)
            )

    async def test_course_video_completion_awards_lesson_tokens(self, db_session):
        """`video_type=COURSE` crossing the threshold must reuse the
        EXISTING `course_service.complete_lesson` reward pathway, per
        this step's own "trigger the pre-existing completion reward
        callbacks if applicable" instruction."""
        user = await _make_user(db_session)
        course, lesson = await _make_course_with_lesson(db_session, duration_seconds=100)
        await _enroll(db_session, user.id, course.id)

        result = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(lesson.id, VideoType.COURSE, current_time=96.0, delta=10.0)
        )

        assert result.is_completed is True
        assert result.tokens_earned == course_service.LESSON_COMPLETION_TOKENS

        # `StudentAccountability.tokens_balance` defaults to 100 for a
        # brand-new account (app/models/gamification.py) -- asserting
        # against that starting balance PLUS the award, not the award
        # amount in isolation, so this test reflects the real balance
        # mechanics rather than assuming a zeroed-out starting point.
        account = await course_service.coach_service.get_or_create_accountability(db_session, user.id)
        assert account.tokens_balance == 100 + course_service.LESSON_COMPLETION_TOKENS

    async def test_course_video_completion_does_not_double_award_on_repeat_heartbeats(self, db_session):
        user = await _make_user(db_session)
        course, lesson = await _make_course_with_lesson(db_session, duration_seconds=100)
        await _enroll(db_session, user.id, course.id)

        first = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(lesson.id, VideoType.COURSE, current_time=96.0, delta=10.0)
        )
        assert first.tokens_earned == course_service.LESSON_COMPLETION_TOKENS

        second = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(lesson.id, VideoType.COURSE, current_time=98.0, delta=10.0)
        )
        assert second.tokens_earned == 0, "an already-completed video must never award a second time"

        account = await course_service.coach_service.get_or_create_accountability(db_session, user.id)
        assert account.tokens_balance == 100 + course_service.LESSON_COMPLETION_TOKENS, "balance must reflect exactly ONE award"

    async def test_course_video_completion_without_enrollment_is_best_effort_noop(self, db_session):
        """A student who is NOT enrolled crossing the threshold (e.g. a
        stray/public video_id) must not fail the heartbeat itself --
        watch-time tracking succeeds; only the reward is skipped."""
        user = await _make_user(db_session)
        _course, lesson = await _make_course_with_lesson(db_session, duration_seconds=100)

        result = await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(lesson.id, VideoType.COURSE, current_time=96.0, delta=10.0)
        )

        assert result.is_completed is True, "watch-time completion tracking must still succeed"
        assert result.tokens_earned == 0, "no reward without a real enrollment"

    async def test_resume_position_defaults_to_zero_with_no_history(self, db_session):
        user = await _make_user(db_session)
        video = await _make_student_video(db_session, user.id, duration_seconds=100)

        progress = await analytics_service.get_playback_progress(db_session, user.id, video.id, VideoType.UGC)

        assert progress.last_position_seconds == 0.0
        assert progress.cumulative_watch_time_seconds == 0.0
        assert progress.is_completed is False

    async def test_resume_position_reflects_latest_heartbeat(self, db_session):
        user = await _make_user(db_session)
        video = await _make_student_video(db_session, user.id, duration_seconds=100)

        await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(video.id, VideoType.UGC, current_time=42.5, delta=10.0)
        )

        progress = await analytics_service.get_playback_progress(db_session, user.id, video.id, VideoType.UGC)

        assert progress.last_position_seconds == pytest.approx(42.5)
        assert progress.cumulative_watch_time_seconds == pytest.approx(10.0)

    async def test_ugc_and_course_playback_logs_for_same_user_are_independent(self, db_session):
        """Two DIFFERENT videos (one UGC, one COURSE) watched by the SAME
        user must never share or clobber each other's row -- exercises
        the polymorphic `(video_id, video_type)` uniqueness pair for
        real, not just `video_id` alone."""
        user = await _make_user(db_session)
        ugc_video = await _make_student_video(db_session, user.id, duration_seconds=100)
        course, lesson = await _make_course_with_lesson(db_session, duration_seconds=200)
        await _enroll(db_session, user.id, course.id)

        await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(ugc_video.id, VideoType.UGC, current_time=5.0, delta=5.0)
        )
        await analytics_service.record_heartbeat(
            db_session, user.id, _heartbeat(lesson.id, VideoType.COURSE, current_time=15.0, delta=15.0)
        )

        ugc_progress = await analytics_service.get_playback_progress(db_session, user.id, ugc_video.id, VideoType.UGC)
        course_progress = await analytics_service.get_playback_progress(
            db_session, user.id, lesson.id, VideoType.COURSE
        )

        assert ugc_progress.cumulative_watch_time_seconds == pytest.approx(5.0)
        assert course_progress.cumulative_watch_time_seconds == pytest.approx(15.0)


# ---------------------------------------------------------------------
# Router-level (real HTTP status code / response envelope) tests
# ---------------------------------------------------------------------


class TestHeartbeatRouterHTTP:
    @pytest.fixture
    def client_factory(self, db_session_factory):
        async def _build():
            async with db_session_factory() as setup_session:
                user = await _make_user(setup_session)

            async def _override_get_db():
                async with db_session_factory() as session:
                    yield session

            async def _override_get_current_user():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_db] = _override_get_db
            app.dependency_overrides[get_current_user] = _override_get_current_user

            return TestClient(app), user

        yield _build
        app.dependency_overrides.clear()

    async def test_heartbeat_endpoint_returns_clean_envelope(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            video = await _make_student_video(session, user.id, duration_seconds=100)

        response = client.post(
            "/api/v1/analytics/heartbeat",
            json={
                "video_id": str(video.id),
                "video_type": "UGC",
                "current_time_seconds": 10.0,
                "delta_duration_seconds": 10.0,
                "is_ended": False,
            },
        )

        assert response.status_code == 200
        body = response.json()
        assert body["video_id"] == str(video.id)
        assert body["video_type"] == "UGC"
        assert body["last_position_seconds"] == pytest.approx(10.0)
        assert body["cumulative_watch_time_seconds"] == pytest.approx(10.0)
        assert body["is_completed"] is False
        assert body["tokens_earned"] == 0

    async def test_heartbeat_endpoint_returns_404_for_unknown_video(self, db_session_factory, client_factory):
        client, _user = await client_factory()

        response = client.post(
            "/api/v1/analytics/heartbeat",
            json={
                "video_id": str(uuid.uuid4()),
                "video_type": "UGC",
                "current_time_seconds": 1.0,
                "delta_duration_seconds": 1.0,
                "is_ended": False,
            },
        )

        assert response.status_code == 404

    async def test_heartbeat_endpoint_rejects_implausible_delta_with_422(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            video = await _make_student_video(session, user.id, duration_seconds=100)

        response = client.post(
            "/api/v1/analytics/heartbeat",
            json={
                "video_id": str(video.id),
                "video_type": "UGC",
                "current_time_seconds": 10.0,
                "delta_duration_seconds": 999999.0,
                "is_ended": False,
            },
        )

        assert response.status_code == 422

    async def test_resume_endpoint_returns_zeros_before_any_heartbeat(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            video = await _make_student_video(session, user.id, duration_seconds=100)

        response = client.get(f"/api/v1/analytics/video/UGC/{video.id}")

        assert response.status_code == 200
        body = response.json()
        assert body["last_position_seconds"] == 0.0
        assert body["cumulative_watch_time_seconds"] == 0.0
        assert body["is_completed"] is False

    async def test_resume_endpoint_reflects_prior_heartbeat(self, db_session_factory, client_factory):
        client, user = await client_factory()
        async with db_session_factory() as session:
            video = await _make_student_video(session, user.id, duration_seconds=100)

        post_response = client.post(
            "/api/v1/analytics/heartbeat",
            json={
                "video_id": str(video.id),
                "video_type": "UGC",
                "current_time_seconds": 33.0,
                "delta_duration_seconds": 10.0,
                "is_ended": False,
            },
        )
        assert post_response.status_code == 200

        get_response = client.get(f"/api/v1/analytics/video/UGC/{video.id}")
        assert get_response.status_code == 200
        body = get_response.json()
        assert body["last_position_seconds"] == pytest.approx(33.0)
        assert body["cumulative_watch_time_seconds"] == pytest.approx(10.0)
