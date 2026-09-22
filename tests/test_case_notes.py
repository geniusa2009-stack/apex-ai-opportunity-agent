"""Behavioral tests for Internal Notes on Cases:

- Any Moderator-or-higher may add/list notes (no tier split, unlike
  the Escalation Path's own raise/resolve split -- see
  `admin_case_notes.py`'s own module docstring).
- A plain student cannot reach any of these routes.
- Notes come back oldest-first, with real resolved author names.
- Delete: the note's own author may delete their own note; a
  DIFFERENT Moderator may not; an Owner may delete anyone's.
- Deleting a note under the wrong item_type/item_id real-404s (can't
  delete a note by guessing its id against an unrelated item).
- Unknown item type / nonexistent item both real-404 for add/list.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite -- same established technique
as `tests/test_moderation_escalations.py`.
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
from app.models.case_note import CaseNote
from app.models.enums import AdminRole
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
    admin_role VARCHAR(20),
    role_granted_at DATETIME,
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER,
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
        await conn.run_sync(Base.metadata.create_all, tables=[CaseNote.__table__])

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None, is_superuser: bool = False, full_name: str = "Test User") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name, admin_role=admin_role, is_superuser=is_superuser)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID) -> StudentVideo:
    video = StudentVideo(id=uuid.uuid4(), student_id=student_id, title="Test Video", status="pending_ai", ai_safety_flag=False)
    db.add(video)
    await db.commit()
    await db.refresh(video)
    return video


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


def _notes_url(video_id: uuid.UUID) -> str:
    return f"/api/v1/admin/moderation/student_video/{video_id}/notes"


class TestCaseNoteTierGating:
    async def test_plain_moderator_can_add_and_list(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        add_resp = client.post(_notes_url(video.id), json={"body": "Checked the transcript, looks fine so far."})
        assert add_resp.status_code == 201
        list_resp = client.get(_notes_url(video.id))
        assert list_resp.status_code == 200 and list_resp.json()["total"] == 1

    async def test_plain_student_cannot_add_or_list(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
        client = client_factory(student)
        assert client.post(_notes_url(video.id), json={"body": "x"}).status_code == 403
        assert client.get(_notes_url(video.id)).status_code == 403

    async def test_unknown_item_type_404(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.post(f"/api/v1/admin/moderation/user/{uuid.uuid4()}/notes", json={"body": "x"})
        assert resp.status_code == 404

    async def test_nonexistent_item_404(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.post(_notes_url(uuid.uuid4()), json={"body": "x"})
        assert resp.status_code == 404


class TestCaseNoteBehavior:
    async def test_multiple_notes_from_different_moderators_are_kept_oldest_first(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            mod_a = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Moderator A")
            mod_b = await _make_user(db, admin_role=AdminRole.MODERATOR, full_name="Moderator B")

        client_factory(mod_a).post(_notes_url(video.id), json={"body": "First note from A."})
        client_b = client_factory(mod_b)
        client_b.post(_notes_url(video.id), json={"body": "Second note from B."})

        list_resp = client_b.get(_notes_url(video.id))
        assert list_resp.status_code == 200
        body = list_resp.json()
        assert body["total"] == 2
        assert [item["body"] for item in body["items"]] == ["First note from A.", "Second note from B."]
        assert [item["author_name"] for item in body["items"]] == ["Moderator A", "Moderator B"]

    async def test_author_can_delete_own_note(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        add_resp = client.post(_notes_url(video.id), json={"body": "my note"})
        note_id = add_resp.json()["id"]

        del_resp = client.delete(f"{_notes_url(video.id)}/{note_id}")
        assert del_resp.status_code == 204
        assert client.get(_notes_url(video.id)).json()["total"] == 0

    async def test_different_moderator_cannot_delete_someone_elses_note(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            mod_a = await _make_user(db, admin_role=AdminRole.MODERATOR)
            mod_b = await _make_user(db, admin_role=AdminRole.MODERATOR)
        add_resp = client_factory(mod_a).post(_notes_url(video.id), json={"body": "A's note"})
        note_id = add_resp.json()["id"]

        client_b = client_factory(mod_b)
        del_resp = client_b.delete(f"{_notes_url(video.id)}/{note_id}")
        assert del_resp.status_code == 403

    async def test_owner_can_delete_anyones_note(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        add_resp = client_factory(moderator).post(_notes_url(video.id), json={"body": "mod's note"})
        note_id = add_resp.json()["id"]

        client_owner = client_factory(owner)
        del_resp = client_owner.delete(f"{_notes_url(video.id)}/{note_id}")
        assert del_resp.status_code == 204

    async def test_delete_under_wrong_item_is_404(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            other_video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        add_resp = client.post(_notes_url(video.id), json={"body": "note on the real video"})
        note_id = add_resp.json()["id"]

        wrong_resp = client.delete(f"{_notes_url(other_video.id)}/{note_id}")
        assert wrong_resp.status_code == 404
        # Still there, unaffected.
        assert client.get(_notes_url(video.id)).json()["total"] == 1

    async def test_delete_nonexistent_note_404(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
            video = await _make_video(db, student_id=student.id)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.delete(f"{_notes_url(video.id)}/{uuid.uuid4()}")
        assert resp.status_code == 404
