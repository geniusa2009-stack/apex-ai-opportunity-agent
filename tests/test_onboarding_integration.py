"""APEX Onboarding Production Integration -- behavioral tests for
`POST /users/me/onboarding/complete` (app/api/v1/endpoints/users.py)
and the two small services it's built on
(`app.services.age`, `app.services.onboarding_taxonomy`).

Real HTTP layer via `TestClient` + `get_current_user`/`get_db`
dependency overrides, in-memory SQLite + `StaticPool` -- same
established convention as `tests/test_onboarding.py` /
`tests/test_personalized_intelligence.py`.

Deliberately a SEPARATE file from the pre-existing `tests/
test_onboarding.py`, which tests the unrelated New-Volunteer Onboarding
Checklist (`app/models/onboarding.py`'s `OnboardingChecklistProgress`)
-- confirmed, during reconnaissance, to share no route prefix, model,
or endpoint with this feature.
"""

from __future__ import annotations

import datetime as dt
import json
import uuid
from unittest.mock import AsyncMock, patch

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
from app.services.age import MIN_SIGNUP_AGE

pytestmark = pytest.mark.asyncio


# ---------------------------------------------------------------------
# SQLite-only test shim for `ARRAY(...)` columns.
# ---------------------------------------------------------------------
# DISCLOSED, TEST-ONLY WORKAROUND: every prior phase's test file that
# needed an `ARRAY(String)` column (`interests`, `application_goals`,
# `preferred_opportunity_types`, ...) under this SQLite fixture wrote
# it via a raw Postgres-array-literal STRING through `text()` SQL, and
# never exercised the real ORM write path (`setattr` + `db.commit()`)
# this endpoint actually uses -- confirmed by grepping every existing
# test file for a `PATCH /users/me`-style call with an array field; none
# exists. SQLAlchemy's `postgresql.ARRAY` type has no SQLite bind/result
# processor at all (it isn't a cross-dialect type), so the FIRST test
# that commits a Python list through the ORM to this SQLite fixture --
# this file -- hits a raw `sqlite3.InterfaceError`. This is a SQLite
# testing-environment limitation, not a production bug: real Postgres
# (via `ARRAY`'s native psycopg support) round-trips a Python list
# through the ORM exactly as `app/api/v1/endpoints/users.py`'s
# `complete_onboarding` writes it, with no special handling needed.
#
# Scope is deliberately narrow: the patch is applied by an `autouse`
# fixture at the start of EVERY test in this file and unconditionally
# reverted in its teardown, so it can never leak into any other test
# file's run in the same pytest process.
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


ENDPOINT = "/api/v1/users/me/onboarding/complete"
TODAY = dt.datetime.now(dt.timezone.utc).date()


def _dob_for_age(age: int, *, month: int = 6, day: int = 15) -> str:
    """A date of birth that computes to exactly `age` years old today,
    using a birthday comfortably in the past relative to `TODAY` so the
    helper itself doesn't need leap-year handling."""
    return dt.date(TODAY.year - age, month, day).isoformat()


def _valid_payload(**overrides) -> dict:
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


# ---------------------------------------------------------------------
# AUTH
# ---------------------------------------------------------------------
class TestAuth:
    async def test_unauthenticated_request_is_rejected(self, client_factory):
        client = client_factory(None)
        response = client.post(ENDPOINT, json=_valid_payload())
        # Same pre-existing HTTPBearer-scheme status-code quirk already
        # disclosed elsewhere in this codebase (tests/test_user_search.py
        # ::test_requires_authentication expects 401, observes 403) --
        # asserting "rejected" rather than one specific code so this test
        # doesn't newly depend on that unrelated, already-known quirk.
        assert response.status_code in (401, 403)

    async def test_authenticated_request_succeeds(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        response = client.post(ENDPOINT, json=_valid_payload())
        assert response.status_code == 200


# ---------------------------------------------------------------------
# DOB / minimum age
# ---------------------------------------------------------------------
class TestDateOfBirth:
    async def test_valid_dob_persists_and_computes_age(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        response = client.post(ENDPOINT, json=_valid_payload(date_of_birth=_dob_for_age(16)))
        assert response.status_code == 200
        body = response.json()
        assert body["profile"]["age"] == 16

    async def test_future_dob_rejected_for_real_user(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        future = (TODAY + dt.timedelta(days=30)).isoformat()
        response = client.post(ENDPOINT, json=_valid_payload(date_of_birth=future))
        assert response.status_code == 422

    async def test_exact_minimum_age_boundary_is_accepted(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        response = client.post(
            ENDPOINT, json=_valid_payload(date_of_birth=_dob_for_age(MIN_SIGNUP_AGE))
        )
        assert response.status_code == 200
        assert response.json()["profile"]["age"] == MIN_SIGNUP_AGE

    async def test_one_year_under_minimum_age_is_rejected(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        response = client.post(
            ENDPOINT, json=_valid_payload(date_of_birth=_dob_for_age(MIN_SIGNUP_AGE - 1))
        )
        assert response.status_code == 422
        async with db_session_factory() as session:
            reloaded = await session.get(User, user.id)
            assert reloaded.onboarding_completed_at is None
            assert reloaded.age is None

    async def test_leap_day_birthday_not_yet_reached_in_non_leap_year(
        self, db_session_factory, client_factory
    ):
        # Someone born Feb 29 of a leap year 13 years before a NON-leap
        # TODAY: if TODAY is before Mar 1, they have not yet had this
        # year's "birthday" and are still 12.
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        # Find the most recent Feb 29 that is >=13 years before today,
        # then check the day immediately before it would "count" this
        # year is handled correctly by comparing against a fixed,
        # deterministic as_of via the age service directly instead of
        # depending on wall-clock TODAY for this edge case.
        from app.services.age import compute_age

        dob = dt.date(2012, 2, 29)
        assert compute_age(dob, as_of=dt.date(2026, 2, 28)) == 13
        assert compute_age(dob, as_of=dt.date(2026, 3, 1)) == 14


# ---------------------------------------------------------------------
# PROFILE persistence + taxonomy mapping
# ---------------------------------------------------------------------
class TestProfilePersistence:
    async def test_full_payload_persists_every_mapped_field(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        response = client.post(
            ENDPOINT,
            json=_valid_payload(
                nationalities=["Egypt", "Sudan"],
                looking_for=["Internships & jobs", "Volunteering"],
                funding="Partially funded is OK",
                where="Online only",
                goal="Win competitions",
            ),
        )
        assert response.status_code == 200
        async with db_session_factory() as session:
            reloaded = await session.get(User, user.id)
            assert reloaded.full_name == "Sara Ahmed"
            assert reloaded.residency_country == "Egypt"
            assert reloaded.nationality == "Egypt"
            assert reloaded.other_nationalities == ["Sudan"]
            assert reloaded.academic_level.value == "secondary_2"
            assert reloaded.english_level.value == "advanced"
            assert reloaded.interests == ["Technology & AI"]
            assert set(reloaded.preferred_opportunity_types) == {
                __import__("app.models.enums", fromlist=["OpportunityType"]).OpportunityType.INTERNSHIP,
                __import__("app.models.enums", fromlist=["OpportunityType"]).OpportunityType.JOB,
                __import__("app.models.enums", fromlist=["OpportunityType"]).OpportunityType.VOLUNTEER,
            }
            assert reloaded.application_goals == ["competitions"]
            assert reloaded.funding_preference == "partial"
            assert reloaded.remote_preference is True
            assert reloaded.onboarding_completed_at is not None

    async def test_invalid_english_level_enum_is_rejected(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        response = client.post(ENDPOINT, json=_valid_payload(english_level="fluentish"))
        assert response.status_code == 422

    async def test_unmapped_taxonomy_values_are_disclosed_not_guessed(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        response = client.post(
            ENDPOINT,
            json=_valid_payload(
                stage="Gap year",
                grade=None,
                looking_for=["Something made up"],
                goal="I'm not sure yet",
            ),
        )
        assert response.status_code == 200
        notes = response.json()["taxonomy_notes"]
        assert notes["unmapped_academic_level"] is True
        assert notes["unmapped_opportunity_types"] == ["Something made up"]
        assert notes["unmapped_goal"] is False  # "I'm not sure yet" IS a recognized, legitimate value
        async with db_session_factory() as session:
            reloaded = await session.get(User, user.id)
            assert reloaded.academic_level is None
            assert reloaded.preferred_opportunity_types is None
            assert reloaded.application_goals is None  # empty list -> None, exploration preserved


# ---------------------------------------------------------------------
# SECURITY
# ---------------------------------------------------------------------
class TestSecurity:
    async def test_cannot_modify_another_users_profile(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            caller = await _make_user(session, full_name="Caller")
            victim = await _make_user(session, full_name="Victim")
        client = client_factory(caller)
        response = client.post(ENDPOINT, json=_valid_payload(first_name="Hacked"))
        assert response.status_code == 200
        async with db_session_factory() as session:
            victim_reloaded = await session.get(User, victim.id)
            assert victim_reloaded.full_name == "Victim"
            caller_reloaded = await session.get(User, caller.id)
            assert caller_reloaded.full_name == "Hacked Ahmed"

    async def test_request_schema_has_no_user_identity_fields(self):
        from app.schemas.user import OnboardingCompleteRequest

        forbidden = {"id", "user_id", "role", "is_superuser", "admin_role", "email"}
        assert forbidden.isdisjoint(OnboardingCompleteRequest.model_fields.keys())

    async def test_extra_smuggled_fields_are_ignored_not_mass_assigned(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)
        payload = _valid_payload()
        payload["role"] = "owner"
        payload["is_superuser"] = True
        response = client.post(ENDPOINT, json=payload)
        assert response.status_code == 200
        async with db_session_factory() as session:
            reloaded = await session.get(User, user.id)
            assert reloaded.is_superuser is False
            assert reloaded.role.value == "student"


# ---------------------------------------------------------------------
# IDEMPOTENCY / retry
# ---------------------------------------------------------------------
class TestIdempotency:
    async def test_repeated_submission_does_not_duplicate_or_reset_completion(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        first = client.post(ENDPOINT, json=_valid_payload())
        assert first.status_code == 200
        first_completed_at = first.json()["onboarding_completed_at"]

        second = client.post(ENDPOINT, json=_valid_payload(interests=["Science & Research"]))
        assert second.status_code == 200
        assert second.json()["onboarding_completed_at"] == first_completed_at

        async with db_session_factory() as session:
            rows = (await session.execute(text("SELECT COUNT(*) FROM users"))).scalar_one()
            assert rows == 1
            reloaded = await session.get(User, user.id)
            assert reloaded.interests == ["Science & Research"]


# ---------------------------------------------------------------------
# AI enrichment
# ---------------------------------------------------------------------
class TestAIEnrichment:
    async def test_valid_extraction_adds_new_tags_additively(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        with patch(
            "app.api.v1.endpoints.users.onboarding_intent_extractor.extract_applicable_tags",
            new=AsyncMock(return_value=["Robotics", "Technology & AI"]),  # one dup-cased, one new
        ):
            response = client.post(
                ENDPOINT,
                json=_valid_payload(
                    interests=["Technology & AI"],
                    intent="I built an Arduino robot and want to find international STEM competitions.",
                ),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ai_enrichment_applied"] is True
        # explicit interest kept, case-insensitive duplicate not re-added, new tag appended
        assert body["profile"]["interests"] == ["Technology & AI", "Robotics"]

    async def test_provider_failure_does_not_block_onboarding(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        with patch(
            "app.api.v1.endpoints.users.onboarding_intent_extractor.extract_applicable_tags",
            new=AsyncMock(side_effect=RuntimeError("provider unavailable")),
        ):
            response = client.post(
                ENDPOINT,
                json=_valid_payload(intent="Something that would trigger extraction."),
            )
        assert response.status_code == 200
        body = response.json()
        assert body["ai_enrichment_applied"] is False
        assert body["profile"]["interests"] == ["Technology & AI"]
        assert body["onboarding_completed_at"] is not None

    async def test_malformed_extraction_result_does_not_block_onboarding(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        with patch(
            "app.api.v1.endpoints.users.onboarding_intent_extractor.extract_applicable_tags",
            new=AsyncMock(side_effect=ValueError("Gemini returned no content")),
        ):
            response = client.post(ENDPOINT, json=_valid_payload(intent="Some intent text."))
        assert response.status_code == 200
        assert response.json()["ai_enrichment_applied"] is False

    async def test_no_intent_text_skips_extraction_entirely(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        with patch(
            "app.api.v1.endpoints.users.onboarding_intent_extractor.extract_applicable_tags",
            new=AsyncMock(return_value=["Should never be called"]),
        ) as mocked:
            response = client.post(ENDPOINT, json=_valid_payload(intent=None))
            mocked.assert_not_called()
        assert response.status_code == 200
        assert response.json()["ai_enrichment_applied"] is False

    async def test_ai_cannot_overwrite_explicit_interests(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            user = await _make_user(session)
        client = client_factory(user)

        with patch(
            "app.api.v1.endpoints.users.onboarding_intent_extractor.extract_applicable_tags",
            new=AsyncMock(return_value=["Medicine & Biology"]),
        ):
            response = client.post(
                ENDPOINT,
                json=_valid_payload(interests=["Technology & AI"], intent="Unrelated AI suggestion test."),
            )
        assert response.status_code == 200
        interests = response.json()["profile"]["interests"]
        # explicit interest is first and still present -- AI only appended
        assert interests[0] == "Technology & AI"
        assert "Medicine & Biology" in interests
