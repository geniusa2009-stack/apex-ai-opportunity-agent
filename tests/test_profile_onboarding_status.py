"""APEX Production Vertical Slice (Signup -> Onboarding -> Intelligence
-> Eligibility -> Feed). Focused regression for the one backend change
this phase made: `UserOut.onboarding_completed_at` (app/schemas/user.py).

Why this field exists: the onboarding frontend (`onboarding.html`) needs
a real, backend-authoritative way to detect "has this user already
completed onboarding" on page load -- `GET /users/me` is the one place
every other page already calls to read the current profile, so exposing
the column there (it already existed on `User`, written by `POST
/users/me/onboarding/complete` since P0-D) is the minimal fix, not a new
field or a new endpoint. Returning users must never be gated on
localStorage alone (see `onboarding.html`'s own module docstring).

Reuses the exact fixtures/shim already proven in
`tests/test_onboarding_integration.py` (same file, not re-derived) --
deliberately a separate, narrow file so a change to this one field's
test never risks touching that file's already-passing 19/19.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.dialects.postgresql import ARRAY as PGArray
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.session import get_db
from app.main import app
from app.models.user import User

pytestmark = pytest.mark.asyncio

_orig_bind_processor = PGArray.bind_processor
_orig_result_processor = PGArray.result_processor


def _sqlite_array_bind_processor(self, dialect):
    if dialect.name != "sqlite":
        return _orig_bind_processor(self, dialect)

    def process(value):
        if value is None:
            return None
        return json.dumps([v.value if hasattr(v, "value") else v for v in value])

    return process


def _sqlite_array_result_processor(self, dialect, coltype):
    if dialect.name != "sqlite":
        return _orig_result_processor(self, dialect, coltype)

    item_type = self.item_type

    def process(value):
        if value is None:
            return None
        raw = json.loads(value)
        try:
            item_processor = item_type.result_processor(dialect, coltype)
        except Exception:
            item_processor = None
        if item_processor:
            return [item_processor(v) for v in raw]
        return raw

    return process


@pytest.fixture(autouse=True)
def _sqlite_array_shim():
    PGArray.bind_processor = _sqlite_array_bind_processor
    PGArray.result_processor = _sqlite_array_result_processor
    yield
    PGArray.bind_processor = _orig_bind_processor
    PGArray.result_processor = _orig_result_processor


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
    remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER,
    date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
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
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, full_name: str = "Test User") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


@pytest.fixture
def client_factory(db_session_factory):
    def _build(user: User | None) -> TestClient:
        async def _override_get_db():
            async with db_session_factory() as session:
                yield session

        app.dependency_overrides[get_db] = _override_get_db
        if user is not None:
            async def _override_get_current_user():
                async with db_session_factory() as session:
                    return await session.get(User, user.id)

            app.dependency_overrides[get_current_user] = _override_get_current_user
        return TestClient(app)

    yield _build
    app.dependency_overrides.clear()


TODAY = dt.datetime.now(dt.timezone.utc).date()


def _dob_for_age(age: int, *, month: int = 6, day: int = 15) -> str:
    return dt.date(TODAY.year - age, month, day).isoformat()


def _valid_onboarding_payload(**overrides) -> dict:
    payload = {
        "date_of_birth": _dob_for_age(16),
        "first_name": "Sara",
        "last_name": "Ahmed",
        "country": "Egypt",
        "nationalities": ["Egypt"],
        "stage": "High school",
        "grade": "Grade 11",
        "english_level": "advanced",
        "interests": ["Technology & AI"],
        "looking_for": ["Competitions"],
        "funding": "Fully funded only",
        "where": "Abroad too",
        "goal": "Study abroad",
        "intent": None,
    }
    payload.update(overrides)
    return payload


class TestOnboardingCompletedAtOnProfileEndpoint:
    async def test_new_user_has_null_onboarding_completed_at(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        response = client.get("/api/v1/users/me")
        assert response.status_code == 200
        assert response.json()["onboarding_completed_at"] is None

    async def test_after_completion_profile_endpoint_reflects_it(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        complete = client.post("/api/v1/users/me/onboarding/complete", json=_valid_onboarding_payload())
        assert complete.status_code == 200, complete.text

        profile = client.get("/api/v1/users/me")
        assert profile.status_code == 200
        body = profile.json()
        assert body["onboarding_completed_at"] is not None
        # Same value the completion call itself returned -- not a second,
        # independently-derived timestamp.
        assert body["onboarding_completed_at"] == complete.json()["onboarding_completed_at"]
