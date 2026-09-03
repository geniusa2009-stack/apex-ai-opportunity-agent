"""Behavioral tests for Canned Responses / Templates:

- Tier gating: create/update/delete are Owner-only; list/use are
  Moderator-or-higher.
- `applies_to` filtering: a scoped filter returns both that item
  type's own templates AND every general-purpose (`applies_to IS
  NULL`) template; an unfiltered list returns everything.
- Update: partial (only sent fields change), and `clear_applies_to`
  really clears back to general-purpose.
- Delete: real 404 on a nonexistent id.
- `use`: increments `times_used` server-side, returns the real body
  text, real 404 if the template no longer exists.

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite.
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
from app.models.canned_response import CannedResponse
from app.models.enums import AdminRole
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
        await conn.run_sync(Base.metadata.create_all, tables=[CannedResponse.__table__])

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, admin_role: AdminRole | None = None, is_superuser: bool = False) -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name="Test User", admin_role=admin_role, is_superuser=is_superuser)
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


class TestCannedResponseTierGating:
    async def test_owner_can_create(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        resp = client.post("/api/v1/admin/canned-responses", json={"title": "Off-topic", "body": "This does not meet our eligibility requirements."})
        assert resp.status_code == 201

    async def test_moderator_cannot_create(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        resp = client.post("/api/v1/admin/canned-responses", json={"title": "x", "body": "y"})
        assert resp.status_code == 403

    async def test_moderator_can_list_and_use(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
            template = CannedResponse(title="Generic", body="Thanks for reporting -- reviewed, no action needed.")
            db.add(template)
            await db.commit()
            await db.refresh(template)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        list_resp = client.get("/api/v1/admin/canned-responses")
        assert list_resp.status_code == 200 and len(list_resp.json()) == 1
        use_resp = client.post(f"/api/v1/admin/canned-responses/{template.id}/use")
        assert use_resp.status_code == 200 and use_resp.json()["times_used"] == 1

    async def test_plain_student_cannot_list(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            student = await _make_user(db)
        client = client_factory(student)
        resp = client.get("/api/v1/admin/canned-responses")
        assert resp.status_code == 403

    async def test_moderator_cannot_update_or_delete(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            template = CannedResponse(title="x", body="y")
            db.add(template)
            await db.commit()
            await db.refresh(template)
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        assert client.patch(f"/api/v1/admin/canned-responses/{template.id}", json={"title": "z"}).status_code == 403
        assert client.delete(f"/api/v1/admin/canned-responses/{template.id}").status_code == 403


class TestCannedResponseBehavior:
    async def test_applies_to_filter_includes_general_purpose(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            general = CannedResponse(title="General", body="general body")
            video_specific = CannedResponse(title="Video-specific", body="video body", applies_to="student_video")
            chat_specific = CannedResponse(title="Chat-specific", body="chat body", applies_to="chat_message_report")
            db.add_all([general, video_specific, chat_specific])
            await db.commit()
            moderator = await _make_user(db, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)

        resp = client.get("/api/v1/admin/canned-responses", params={"applies_to": "student_video"})
        assert resp.status_code == 200
        titles = {item["title"] for item in resp.json()}
        assert titles == {"General", "Video-specific"}

        resp_all = client.get("/api/v1/admin/canned-responses")
        assert resp_all.status_code == 200 and len(resp_all.json()) == 3

    async def test_update_is_partial_and_clear_applies_to(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
            template = CannedResponse(title="Old title", body="Old body", applies_to="student_video")
            db.add(template)
            await db.commit()
            await db.refresh(template)
        client = client_factory(owner)

        resp = client.patch(f"/api/v1/admin/canned-responses/{template.id}", json={"title": "New title"})
        assert resp.status_code == 200
        body = resp.json()
        assert body["title"] == "New title"
        assert body["body"] == "Old body"
        assert body["applies_to"] == "student_video"

        resp2 = client.patch(f"/api/v1/admin/canned-responses/{template.id}", json={"clear_applies_to": True})
        assert resp2.status_code == 200 and resp2.json()["applies_to"] is None

    async def test_delete_then_use_is_real_404(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
            template = CannedResponse(title="x", body="y")
            db.add(template)
            await db.commit()
            await db.refresh(template)
        client = client_factory(owner)

        del_resp = client.delete(f"/api/v1/admin/canned-responses/{template.id}")
        assert del_resp.status_code == 204

        use_resp = client.post(f"/api/v1/admin/canned-responses/{template.id}/use")
        assert use_resp.status_code == 404

        update_resp = client.patch(f"/api/v1/admin/canned-responses/{template.id}", json={"title": "z"})
        assert update_resp.status_code == 404

    async def test_using_twice_increments_correctly(self, client_factory, db_session_factory):
        async with db_session_factory() as db:
            owner = await _make_user(db, is_superuser=True, admin_role=AdminRole.OWNER)
            template = CannedResponse(title="x", body="y")
            db.add(template)
            await db.commit()
            await db.refresh(template)
        client = client_factory(owner)
        r1 = client.post(f"/api/v1/admin/canned-responses/{template.id}/use")
        r2 = client.post(f"/api/v1/admin/canned-responses/{template.id}/use")
        assert r1.json()["times_used"] == 1
        assert r2.json()["times_used"] == 2
