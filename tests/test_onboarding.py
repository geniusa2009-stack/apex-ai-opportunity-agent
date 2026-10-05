"""Behavioral tests for the New-Volunteer Onboarding Checklist step:
`role_granted_at` on `User` + the reset-on-fresh-grant logic in
`assign_admin_role` (app/api/v1/endpoints/admin_roles.py), and the three
new onboarding endpoints (`app/api/v1/endpoints/onboarding.py`).

Real HTTP layer, via `TestClient` + `get_current_user`/`get_db`
dependency overrides -- same technique as `tests/test_admin_roles.py`.
In-memory SQLite, `StaticPool`.
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
from app.models.enums import AdminRole
from app.models.onboarding import ONBOARDING_STEPS, OnboardingChecklistProgress
from app.models.practice_case import PRACTICE_ACTIONS_BY_CASE_TYPE, PRACTICE_CASES, PracticeCaseAttempt
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
    nationality VARCHAR(120), residency_country VARCHAR(120), languages TEXT, interests TEXT, application_goals TEXT, preferred_opportunity_types TEXT, remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
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
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[OnboardingChecklistProgress.__table__, PracticeCaseAttempt.__table__],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession,
    *,
    is_superuser: bool = False,
    admin_role: AdminRole | None = None,
    role_granted_at=None,
    full_name: str = "Test User",
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        is_superuser=is_superuser,
        admin_role=admin_role,
        role_granted_at=role_granted_at,
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
# `assign_admin_role`'s new role_granted_at / checklist-reset behavior.
# ---------------------------------------------------------------------


class TestRoleGrantInitializesChecklist:
    async def test_first_time_grant_stamps_role_granted_at(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, full_name="New Volunteer")

        client = client_factory(owner)
        response = client.patch(f"/api/v1/admin/users/{target.id}/role", json={"new_role": "moderator"})
        assert response.status_code == 200

        async with db_session_factory() as session:
            persisted = await session.get(User, target.id)
            assert persisted.role_granted_at is not None

    async def test_first_time_grant_resets_stale_progress(self, db_session_factory, client_factory):
        """A user who somehow already has completed-step rows (e.g. a
        prior tenure, then revoked, then re-invited later) gets a
        genuinely FRESH checklist on the new grant -- real, disclosed
        reset behavior, not stale carry-over."""
        step_key = ONBOARDING_STEPS[0].key
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, full_name="Returning Volunteer")
            session.add(OnboardingChecklistProgress(user_id=target.id, step_key=step_key))
            await session.commit()

        client = client_factory(owner)
        response = client.patch(f"/api/v1/admin/users/{target.id}/role", json={"new_role": "moderator"})
        assert response.status_code == 200

        async with db_session_factory() as session:
            result = await session.execute(
                text("SELECT COUNT(*) FROM onboarding_checklist_progress WHERE user_id = :uid"),
                {"uid": target.id.hex},
            )
            assert result.scalar_one() == 0

    async def test_lateral_tier_change_does_not_reset_role_granted_at(self, db_session_factory, client_factory):
        """MODERATOR -> SENIOR_MODERATOR is a re-tier, not a new-volunteer
        event -- `role_granted_at` and any existing progress must NOT be
        touched. Compares two DB-round-tripped values (rather than
        against a Python-constructed literal) to sidestep SQLite's own
        tz-naive-on-read behavior for `DateTime(timezone=True)` columns
        -- the real, honest assertion is "unchanged by this call," not
        "byte-identical to what Python originally wrote.\""""
        import datetime as dt

        step_key = ONBOARDING_STEPS[0].key
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(
                session,
                admin_role=AdminRole.MODERATOR,
                role_granted_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
                full_name="Veteran Mod",
            )
            session.add(OnboardingChecklistProgress(user_id=target.id, step_key=step_key))
            await session.commit()
            original_grant = (await session.get(User, target.id)).role_granted_at

        client = client_factory(owner)
        response = client.patch(f"/api/v1/admin/users/{target.id}/role", json={"new_role": "senior_moderator"})
        assert response.status_code == 200

        async with db_session_factory() as session:
            persisted = await session.get(User, target.id)
            assert persisted.role_granted_at == original_grant

            result = await session.execute(
                text("SELECT COUNT(*) FROM onboarding_checklist_progress WHERE user_id = :uid"),
                {"uid": target.id.hex},
            )
            assert result.scalar_one() == 1

    async def test_revocation_clears_role_granted_at(self, db_session_factory, client_factory):
        import datetime as dt

        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(
                session,
                admin_role=AdminRole.MODERATOR,
                role_granted_at=dt.datetime(2026, 1, 1, tzinfo=dt.timezone.utc),
            )

        client = client_factory(owner)
        response = client.patch(f"/api/v1/admin/users/{target.id}/role", json={"new_role": None})
        assert response.status_code == 200

        async with db_session_factory() as session:
            persisted = await session.get(User, target.id)
            assert persisted.role_granted_at is None


# ---------------------------------------------------------------------
# `GET /me/onboarding` + mark/unmark -- own record only.
# ---------------------------------------------------------------------


class TestOwnOnboardingChecklist:
    async def test_plain_student_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)
        client = client_factory(student)
        response = client.get("/api/v1/me/onboarding")
        assert response.status_code == 403

    async def test_fresh_moderator_sees_all_steps_incomplete(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/me/onboarding")
        assert response.status_code == 200
        body = response.json()
        assert len(body["steps"]) == len(ONBOARDING_STEPS)
        assert all(not s["completed"] for s in body["steps"])
        assert body["all_completed"] is False

    async def test_mark_step_complete_persists(self, db_session_factory, client_factory):
        step_key = ONBOARDING_STEPS[0].key
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(moderator)
        response = client.post(f"/api/v1/me/onboarding/{step_key}/complete")
        assert response.status_code == 200
        assert response.json()["completed"] is True

        checklist = client.get("/api/v1/me/onboarding").json()
        marked = next(s for s in checklist["steps"] if s["key"] == step_key)
        assert marked["completed"] is True
        assert marked["completed_at"] is not None

    async def test_mark_step_complete_twice_is_idempotent(self, db_session_factory, client_factory):
        step_key = ONBOARDING_STEPS[0].key
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        assert client.post(f"/api/v1/me/onboarding/{step_key}/complete").status_code == 200
        assert client.post(f"/api/v1/me/onboarding/{step_key}/complete").status_code == 200

        async with db_session_factory() as session:
            result = await session.execute(text("SELECT COUNT(*) FROM onboarding_checklist_progress"))
            assert result.scalar_one() == 1

    async def test_unmark_step_removes_completion(self, db_session_factory, client_factory):
        step_key = ONBOARDING_STEPS[0].key
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        client.post(f"/api/v1/me/onboarding/{step_key}/complete")
        response = client.delete(f"/api/v1/me/onboarding/{step_key}/complete")
        assert response.status_code == 200
        assert response.json()["completed"] is False

        checklist = client.get("/api/v1/me/onboarding").json()
        marked = next(s for s in checklist["steps"] if s["key"] == step_key)
        assert marked["completed"] is False

    async def test_unknown_step_key_404(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.post("/api/v1/me/onboarding/not_a_real_step/complete")
        assert response.status_code == 404

    async def test_all_completed_true_once_every_step_marked(self, db_session_factory, client_factory):
        """Training / Practice Mode for New Volunteers addition: the
        real sixth step (`complete_practice_cases`) can never be marked
        via this endpoint (it 400s -- see the dedicated rejection test
        below), so genuinely reaching `all_completed=True` now also
        needs a real practice attempt recorded for every real case in
        the real practice-case bank, exactly mirroring what a real
        volunteer would actually do. `tests/test_practice_cases.py`
        owns the full behavioral coverage of the practice endpoints
        themselves; this test only needs the real end state to prove
        the two features' real integration."""
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        for step in ONBOARDING_STEPS:
            client.post(f"/api/v1/me/onboarding/{step.key}/complete")
        for case in PRACTICE_CASES:
            action = PRACTICE_ACTIONS_BY_CASE_TYPE[case.case_type][0]
            resp = client.post(f"/api/v1/me/practice/{case.key}/act", json={"action": action})
            assert resp.status_code == 200
        response = client.get("/api/v1/me/onboarding")
        assert response.json()["all_completed"] is True

    async def test_volunteer_cannot_mark_another_volunteers_progress(self, db_session_factory, client_factory):
        """There is no `user_id` parameter anywhere on these endpoints --
        the only way to test this is to confirm B's mark is invisible on
        A's own checklist (A has no route that could even target B)."""
        step_key = ONBOARDING_STEPS[0].key
        async with db_session_factory() as session:
            volunteer_a = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="A")
            volunteer_b = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="B")

        client_b = client_factory(volunteer_b)
        assert client_b.post(f"/api/v1/me/onboarding/{step_key}/complete").status_code == 200

        client_a = client_factory(volunteer_a)
        checklist_a = client_a.get("/api/v1/me/onboarding").json()
        marked = next(s for s in checklist_a["steps"] if s["key"] == step_key)
        assert marked["completed"] is False

    async def test_practice_step_cannot_be_self_marked_or_unmarked(self, db_session_factory, client_factory):
        """Training / Practice Mode for New Volunteers addition: the
        real sixth step is server-computed from real `PracticeCaseAttempt`
        rows -- a direct self-report attempt on it is a real, honest
        400, never silently accepted (see `onboarding.py`'s own
        rejection in `complete_my_onboarding_step`/
        `uncomplete_my_onboarding_step`)."""
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)

        complete_resp = client.post("/api/v1/me/onboarding/complete_practice_cases/complete")
        assert complete_resp.status_code == 400

        uncomplete_resp = client.delete("/api/v1/me/onboarding/complete_practice_cases/complete")
        assert uncomplete_resp.status_code == 400

        checklist = client.get("/api/v1/me/onboarding").json()
        practice_step = next(s for s in checklist["steps"] if s["key"] == "complete_practice_cases")
        assert practice_step["completed"] is False


# ---------------------------------------------------------------------
# `GET /admin/onboarding/team` -- Owner-only, whole-team visibility.
# ---------------------------------------------------------------------


class TestTeamOnboardingView:
    async def test_moderator_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/onboarding/team")
        assert response.status_code == 403

    async def test_owner_sees_whole_team_progress(self, db_session_factory, client_factory):
        step_key = ONBOARDING_STEPS[0].key
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Real Mod")
            session.add(OnboardingChecklistProgress(user_id=moderator.id, step_key=step_key))
            await session.commit()

        client = client_factory(owner)
        response = client.get("/api/v1/admin/onboarding/team")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 1  # is_superuser-only Owner excluded, per this endpoint's own docstring
        member = body["items"][0]
        assert member["full_name"] == "Real Mod"
        assert member["completed_count"] == 1
        assert member["total_steps"] == len(ONBOARDING_STEPS)
        assert member["all_completed"] is False

    async def test_legacy_owner_excluded_from_team_onboarding_view(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER, full_name="Founder")

        client = client_factory(owner)
        response = client.get("/api/v1/admin/onboarding/team")
        assert response.status_code == 200
        body = response.json()
        assert body["total"] == 0
        assert all(item["full_name"] != "Founder" for item in body["items"])
