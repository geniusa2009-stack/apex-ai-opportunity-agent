"""Behavioral test for the Real-time Student UGC Video Upload Tracker &
Feedback Dashboard step: `GET /student/videos/my-uploads`
(`app/api/v1/endpoints/student_videos.py`).

Real HTTP via `TestClient`, with `get_current_user`/`get_db`
dependency-overridden -- same technique as `tests/test_courses_enrollment.py`.
Reuses that file's raw-SQL `users` table (no `ARRAY` column issue here
either, but `student_videos` itself needs the SAME raw-SQL treatment as
`tests/test_student_videos.py` uses, for the same reason: `ai_feedback`
is `JSONB`, which SQLite's DDL compiler can't render).
"""

import uuid
from datetime import datetime, timedelta, timezone

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.enums import StudentVideoStatus
from app.models.student_videos import StudentVideo
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
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""
# `thumbnail_url` (renamed from `thumbnail_cid`) added here directly
# alongside its own step (Hybrid Video Thumbnail System, migration 0016)
# -- same disclosed "update this file's raw SQL in the SAME pass that
# adds the column to the real model" discipline established after the
# regression found in `tests/test_student_videos.py` during the
# immediately preceding step.
#
# `admin_override`/`admin_notes` added here the same way, directly
# alongside that later step (Admin Override Dashboard & AI Decision
# Correction System, migration 0017) -- continuing that same discipline
# rather than letting it drift again.
#
# `hls_master_url`/`thumbnail_url` RENAMED from `hls_master_cid`/
# `thumbnail_cid` (Student Video Pinata -> Cloudflare R2 Migration,
# migration 0018), widened to `VARCHAR(500)` to match the real model --
# same continuing discipline, updated in the SAME pass as the real
# column rename.


@pytest_asyncio.fixture
async def db_session_factory():
    engine = create_async_engine("sqlite+aiosqlite:///:memory:", poolclass=StaticPool)
    async with engine.begin() as conn:
        await conn.exec_driver_sql(_CREATE_USERS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_USER_BOOKMARKS_TABLE_SQL)
        await conn.exec_driver_sql(_CREATE_STUDENT_VIDEOS_TABLE_SQL)

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test Student")
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _insert_video(db: AsyncSession, *, student_id, title, status, created_at, **overrides) -> uuid.UUID:
    """Inserts via the ORM (`StudentVideo(...)` + `session.add`), NOT raw
    SQL -- same proven pattern `tests/test_student_videos.py` already
    established for this exact table: `ai_feedback` (a `JSONB`-typed
    column) needs to go through SQLAlchemy's own bind/result processors
    to round-trip as a real Python `dict` on read, which only happens
    for ORM-mediated writes, not a hand-written raw `INSERT`.
    """
    video_id = uuid.uuid4()
    video = StudentVideo(
        id=video_id,
        student_id=student_id,
        title=title,
        status=status,
        created_at=created_at,
        ai_safety_flag=overrides.get("ai_safety_flag", False),
        ai_score=overrides.get("ai_score"),
        hls_master_url=overrides.get("hls_master_url"),
        ai_feedback=overrides.get("ai_feedback"),
    )
    db.add(video)
    await db.commit()
    return video_id


class TestMyUploadsEndpoint:
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

    async def test_returns_only_own_videos_all_statuses_sorted_newest_first(
        self, db_session_factory, client_factory
    ):
        client, user = await client_factory()
        other_student_id = uuid.uuid4()

        now = datetime.now(timezone.utc)
        async with db_session_factory() as session:
            await _insert_video(
                session, student_id=user.id, title="Oldest", status=StudentVideoStatus.APPROVED.value,
                created_at=now - timedelta(hours=3), ai_score=88,
                hls_master_url="https://pub-test.r2.dev/student-videos/oldest/hls/master.m3u8",
            )
            await _insert_video(
                session, student_id=user.id, title="Middle (rejected)", status=StudentVideoStatus.REJECTED.value,
                created_at=now - timedelta(hours=2), ai_score=30,
                ai_feedback={
                    "summary": "Off-topic content.",
                    "strengths": ["Clear audio", "Good pacing"],
                    "growth_points": ["Stay on the assigned topic", "Cite a source"],
                    "tags": ["off-topic", "needs-work", "audio-ok"],
                },
            )
            await _insert_video(
                session, student_id=user.id, title="Newest (pending)", status=StudentVideoStatus.PENDING_AI.value,
                created_at=now - timedelta(minutes=5),
            )
            # Belongs to a DIFFERENT student -- must never appear in the response.
            await _insert_video(
                session, student_id=other_student_id, title="Someone else's video",
                status=StudentVideoStatus.APPROVED.value, created_at=now,
            )

        response = client.get("/api/v1/student/videos/my-uploads")

        assert response.status_code == 200
        body = response.json()

        assert len(body) == 3, "must return exactly this student's 3 videos, not the other student's 4th"
        assert [v["title"] for v in body] == ["Newest (pending)", "Middle (rejected)", "Oldest"], (
            "must be sorted by created_at DESC"
        )

        pending = body[0]
        assert pending["status"] == "pending_ai"
        assert pending["ai_score"] is None
        assert pending["ai_feedback"] is None

        rejected = body[1]
        assert rejected["status"] == "rejected"
        assert rejected["ai_score"] == 30
        assert rejected["ai_feedback"]["summary"] == "Off-topic content."
        assert len(rejected["ai_feedback"]["growth_points"]) == 2

        approved = body[2]
        assert approved["status"] == "approved"
        assert approved["ai_score"] == 88
        assert approved["hls_master_url"] == "https://pub-test.r2.dev/student-videos/oldest/hls/master.m3u8"
