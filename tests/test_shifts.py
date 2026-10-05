"""Behavioral tests for the Volunteer Coverage / Shift Schedule step:
`ShiftAssignment` (app/models/shift.py), CRUD + overlap detection
(app/services/shift_service.py), and the real endpoints
(app/api/v1/endpoints/admin_shifts.py).

Real HTTP layer, via TestClient + get_current_user/get_db dependency
overrides -- same technique as tests/test_onboarding.py. In-memory
SQLite, StaticPool.
"""

import datetime as dt
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
from app.models.shift import ShiftAssignment
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
        await conn.run_sync(Base.metadata.create_all, tables=[ShiftAssignment.__table__])

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(
    db: AsyncSession,
    *,
    is_superuser: bool = False,
    admin_role: AdminRole | None = None,
    full_name: str = "Test User",
) -> User:
    user = User(
        id=uuid.uuid4(),
        email=f"{uuid.uuid4()}@example.com",
        full_name=full_name,
        is_superuser=is_superuser,
        admin_role=admin_role,
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


def _iso(d: dt.datetime) -> str:
    return d.isoformat()


NOW = dt.datetime(2026, 9, 10, 12, 0, 0, tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------------
# Tier gating: write vs. read.
# ---------------------------------------------------------------------


class TestShiftTierGating:
    async def test_moderator_cannot_create_shift(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Target")

        client = client_factory(moderator)
        response = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )
        assert response.status_code == 403

    async def test_plain_student_cannot_read_team_schedule(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)

        client = client_factory(student)
        response = client.get("/api/v1/admin/shifts")
        assert response.status_code == 403

    async def test_moderator_can_read_team_schedule(self, db_session_factory, client_factory):
        """Deliberately BROADER than GET /admin/team's own Owner-only
        roster -- see admin_shifts.py's own module docstring for the
        real reasoning."""
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(moderator)
        response = client.get("/api/v1/admin/shifts")
        assert response.status_code == 200

    async def test_owner_can_create_shift(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Target")

        client = client_factory(owner)
        response = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )
        assert response.status_code == 201
        body = response.json()
        assert body["shift"]["volunteer_id"] == str(target.id)
        assert body["overlapping_shift_ids"] == []


# ---------------------------------------------------------------------
# CRUD.
# ---------------------------------------------------------------------


class TestShiftCrud:
    async def test_create_reject_a_plain_student_as_volunteer(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            student = await _make_user(session, full_name="Plain Student")

        client = client_factory(owner)
        response = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(student.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=1))},
        )
        assert response.status_code == 404

    async def test_create_rejects_end_before_start(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        response = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW - dt.timedelta(hours=1))},
        )
        assert response.status_code == 422

    async def test_update_persists_and_returns_fresh_overlap_check(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        create_res = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2)), "notes": "original"},
        )
        shift_id = create_res.json()["shift"]["id"]

        update_res = client.patch(f"/api/v1/admin/shifts/{shift_id}", json={"notes": "updated note"})
        assert update_res.status_code == 200
        assert update_res.json()["shift"]["notes"] == "updated note"
        # start_at/end_at untouched by a notes-only PATCH.
        assert update_res.json()["shift"]["start_at"] is not None

    async def test_update_can_clear_notes_to_null(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        create_res = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2)), "notes": "original"},
        )
        shift_id = create_res.json()["shift"]["id"]

        update_res = client.patch(f"/api/v1/admin/shifts/{shift_id}", json={"notes": None})
        assert update_res.status_code == 200
        assert update_res.json()["shift"]["notes"] is None

    async def test_delete_really_removes_the_shift(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        create_res = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )
        shift_id = create_res.json()["shift"]["id"]

        delete_res = client.delete(f"/api/v1/admin/shifts/{shift_id}")
        assert delete_res.status_code == 204

        list_res = client.get("/api/v1/admin/shifts?include_past=true")
        assert shift_id not in [item["id"] for item in list_res.json()["items"]]

    async def test_delete_unknown_shift_404s(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)

        client = client_factory(owner)
        response = client.delete(f"/api/v1/admin/shifts/{uuid.uuid4()}")
        assert response.status_code == 404


# ---------------------------------------------------------------------
# Overlap handling: allowed + flagged, scoped to the SAME volunteer.
# ---------------------------------------------------------------------


class TestShiftOverlapHandling:
    async def test_overlapping_shift_for_same_volunteer_is_allowed_and_flagged(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        first_res = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=3))},
        )
        first_id = first_res.json()["shift"]["id"]

        # Overlaps [NOW, NOW+3h) by 1 hour.
        second_res = client.post(
            "/api/v1/admin/shifts",
            json={
                "volunteer_id": str(target.id),
                "start_at": _iso(NOW + dt.timedelta(hours=2)),
                "end_at": _iso(NOW + dt.timedelta(hours=4)),
            },
        )
        assert second_res.status_code == 201, "overlaps are ALLOWED, not blocked"
        assert second_res.json()["overlapping_shift_ids"] == [first_id]

    async def test_overlapping_shift_for_different_volunteers_is_not_flagged(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target_a = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="A")
            target_b = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR, full_name="B")

        client = client_factory(owner)
        client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target_a.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=3))},
        )
        # SAME window, DIFFERENT volunteer -- real, intentional double
        # coverage, not flagged at all.
        second_res = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target_b.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=3))},
        )
        assert second_res.status_code == 201
        assert second_res.json()["overlapping_shift_ids"] == []

    async def test_adjacent_shifts_do_not_overlap(self, db_session_factory, client_factory):
        """[NOW, NOW+2h) and [NOW+2h, NOW+4h) touch at the boundary but
        do not overlap -- half-open interval semantics."""
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )
        second_res = client.post(
            "/api/v1/admin/shifts",
            json={
                "volunteer_id": str(target.id),
                "start_at": _iso(NOW + dt.timedelta(hours=2)),
                "end_at": _iso(NOW + dt.timedelta(hours=4)),
            },
        )
        assert second_res.status_code == 201
        assert second_res.json()["overlapping_shift_ids"] == []

    async def test_update_moving_shift_onto_another_flags_it(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        first_res = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )
        first_id = first_res.json()["shift"]["id"]
        second_res = client.post(
            "/api/v1/admin/shifts",
            json={
                "volunteer_id": str(target.id),
                "start_at": _iso(NOW + dt.timedelta(hours=10)),
                "end_at": _iso(NOW + dt.timedelta(hours=12)),
            },
        )
        second_id = second_res.json()["shift"]["id"]

        # Move the second shift to overlap the first.
        update_res = client.patch(
            f"/api/v1/admin/shifts/{second_id}",
            json={"start_at": _iso(NOW + dt.timedelta(hours=1)), "end_at": _iso(NOW + dt.timedelta(hours=3))},
        )
        assert update_res.status_code == 200
        assert update_res.json()["overlapping_shift_ids"] == [first_id]

    async def test_update_does_not_flag_a_shift_against_its_own_prior_self(self, db_session_factory, client_factory):
        """A no-op-ish edit (e.g. just changing notes) must not report
        the shift as overlapping ITSELF."""
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)

        client = client_factory(owner)
        create_res = client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )
        shift_id = create_res.json()["shift"]["id"]

        update_res = client.patch(f"/api/v1/admin/shifts/{shift_id}", json={"notes": "just a note"})
        assert update_res.status_code == 200
        assert update_res.json()["overlapping_shift_ids"] == []


# ---------------------------------------------------------------------
# include_past filtering.
# ---------------------------------------------------------------------


class TestShiftListFiltering:
    async def test_past_shift_excluded_by_default(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR)
            past_start = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=10)
            session.add(ShiftAssignment(volunteer_id=target.id, start_at=past_start, end_at=past_start + dt.timedelta(hours=1)))
            await session.commit()

        client = client_factory(owner)
        default_res = client.get("/api/v1/admin/shifts")
        assert default_res.json()["total"] == 0

        past_res = client.get("/api/v1/admin/shifts?include_past=true")
        assert past_res.json()["total"] == 1


# ---------------------------------------------------------------------
# .ics export.
# ---------------------------------------------------------------------


class TestShiftIcsExport:
    async def test_export_ics_contains_real_event_with_start_and_end(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Export Volunteer")

        client = client_factory(owner)
        client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2)), "notes": "cover the queue"},
        )

        response = client.get("/api/v1/admin/shifts/export.ics")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/calendar")
        body = response.text
        assert "BEGIN:VCALENDAR" in body
        assert "BEGIN:VEVENT" in body
        assert "DTSTART:" in body
        assert "DTEND:" in body
        assert "Export Volunteer" in body
        assert "cover the queue" in body

    async def test_export_ics_mine_only_filters_to_caller(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            target_a = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Volunteer A")
            target_b = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Volunteer B")

        owner_client = client_factory(owner)
        owner_client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target_a.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )
        owner_client.post(
            "/api/v1/admin/shifts",
            json={"volunteer_id": str(target_b.id), "start_at": _iso(NOW), "end_at": _iso(NOW + dt.timedelta(hours=2))},
        )

        a_client = client_factory(target_a)
        response = a_client.get("/api/v1/admin/shifts/export.ics?mine_only=true")
        body = response.text
        assert "Volunteer A" in body
        assert "Volunteer B" not in body

    async def test_moderator_cannot_export_ics_denied_for_plain_student(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            student = await _make_user(session)

        client = client_factory(student)
        response = client.get("/api/v1/admin/shifts/export.ics")
        assert response.status_code == 403

