"""Behavioral tests for Export Moderation Log:
`GET /admin/activity-log/export` (app/api/v1/endpoints/admin_activity_log.py).

Same real-HTTP-layer technique as `tests/test_moderation_log.py`
(`TestActivityLogReadEndpoints`) -- `TestClient` + `get_current_user`/
`get_db` dependency overrides, in-memory SQLite via `StaticPool`, a raw
`moderation_log_entries` table (this model's `outcome` column is
`JSONB`, which SQLite can't compile via the ORM -- same reason
`test_moderation_log.py`'s own fixture uses raw SQL for this table).

Covers: tier gating, real CSV structure/content against known seeded
data, the polymorphic target_type/target_id + JSONB outcome flattening,
date-range filtering (including the DEFAULT_EXPORT_RANGE_DAYS fallback),
the MAX_EXPORT_ROWS/MAX_EXPORT_RANGE_DAYS limits, and the export's own
self-logging (Step 3.3).
"""

import csv
import io
import json
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.api.v1.endpoints import admin_activity_log as export_module
from app.db.session import get_db
from app.main import app
from app.models.enums import AdminRole, ModerationAction, ModerationTargetType
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

# The `User` model's own relationships (bookmarks, etc.) get touched
# during dependency resolution even though this file's own tests never
# query them directly -- same full table set `test_moderation_log.py`'s
# own fixture creates, for the same reason (a bare `users` table alone
# is not enough; SQLAlchemy needs every table a `User` relationship
# could touch to exist, or a plain `session.get(User, ...)` in the
# `client_factory` override below raises `OperationalError: no such
# table`).
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
        await conn.execute(text(_CREATE_MODERATION_LOG_TABLE_SQL))

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
        id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name,
        is_superuser=is_superuser, admin_role=admin_role,
    )
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_log_entry(
    db: AsyncSession,
    *,
    actor_id: uuid.UUID,
    actor_role: AdminRole,
    action: ModerationAction,
    target_type: ModerationTargetType,
    target_id: uuid.UUID,
    outcome: dict,
    created_at: str | None = None,
) -> None:
    """Same raw-SQL seed helper `test_moderation_log.py::_make_log_entry`
    uses, for the same reason (SQLite can't compile the real `JSONB`
    column type via the ORM)."""
    await db.execute(
        text(
            """INSERT INTO moderation_log_entries
            (id, actor_id, actor_role, action, target_type, target_id, outcome, created_at)
            VALUES (:id, :actor_id, :actor_role, :action, :target_type, :target_id, :outcome,
                    COALESCE(:created_at, CURRENT_TIMESTAMP))"""
        ),
        {
            "id": uuid.uuid4().hex,
            "actor_id": actor_id.hex,
            "actor_role": actor_role.value,
            "action": action.value,
            "target_type": target_type.value,
            "target_id": target_id.hex,
            "outcome": json.dumps(outcome),
            "created_at": created_at,
        },
    )
    await db.commit()


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


def _parse_csv(content: bytes) -> list[list[str]]:
    text_content = content.decode("utf-8")
    return list(csv.reader(io.StringIO(text_content)))


class TestTierGating:
    async def test_moderator_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
        client = client_factory(moderator)
        response = client.get("/api/v1/admin/activity-log/export")
        assert response.status_code == 403
        assert response.json()["detail"] == "Requires owner access or higher."

    async def test_senior_moderator_403(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            senior = await _make_user(session, admin_role=AdminRole.SENIOR_MODERATOR)
        client = client_factory(senior)
        response = client.get("/api/v1/admin/activity-log/export")
        assert response.status_code == 403

    async def test_owner_200(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export")
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/csv")


class TestCsvStructureAndContent:
    async def test_header_row_and_known_row_content(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR, full_name="Mod Person")
            video_id = uuid.uuid4()
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.VIDEO_OVERRIDE, target_type=ModerationTargetType.STUDENT_VIDEO,
                target_id=video_id, outcome={"new_status": "approved"},
                created_at="2026-06-15 10:00:00",
            )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-12-31T00:00:00")
        assert response.status_code == 200

        rows = _parse_csv(response.content)
        assert rows[0] == [
            "Timestamp (UTC)", "Actor Email", "Actor Full Name", "Actor Role",
            "Action", "Target Type", "Target ID", "Outcome (JSON)",
        ]
        assert len(rows) == 2  # header + 1 real row
        data_row = rows[1]
        assert data_row[2] == "Mod Person"
        assert data_row[3] == "moderator"
        assert data_row[4] == "video_override"
        assert data_row[5] == "student_video"
        assert data_row[6] == str(video_id)
        assert json.loads(data_row[7]) == {"new_status": "approved"}

    async def test_content_disposition_attachment_filename(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-02-01T00:00:00")
        assert response.status_code == 200
        disposition = response.headers["content-disposition"]
        assert disposition.startswith("attachment;")
        assert "moderation_log_2026-01-01_to_2026-02-01.csv" in disposition

    async def test_empty_range_yields_header_only_csv(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-02-01T00:00:00")
        assert response.status_code == 200
        rows = _parse_csv(response.content)
        assert len(rows) == 1  # header row only, real empty result honestly rendered


class TestPolymorphicAndJsonFieldHandling:
    async def test_different_target_types_and_outcome_shapes_all_render(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            user_target = uuid.uuid4()
            opp_target = uuid.uuid4()
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_MANUAL_BAN, target_type=ModerationTargetType.USER,
                target_id=user_target, outcome={"duration_hours": 24, "reason": "spam"},
                created_at="2026-06-01 00:00:00",
            )
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.OPPORTUNITY_REJECT, target_type=ModerationTargetType.OPPORTUNITY,
                target_id=opp_target, outcome={"notes": None},
                created_at="2026-06-02 00:00:00",
            )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-12-31T00:00:00")
        rows = _parse_csv(response.content)
        assert len(rows) == 3  # header + 2 rows

        by_target_type = {r[5]: r for r in rows[1:]}
        assert by_target_type["user"][6] == str(user_target)
        assert json.loads(by_target_type["user"][7]) == {"duration_hours": 24, "reason": "spam"}
        assert by_target_type["opportunity"][6] == str(opp_target)
        assert json.loads(by_target_type["opportunity"][7]) == {"notes": None}

    async def test_outcome_with_embedded_comma_and_quote_survives_real_csv_round_trip(self, db_session_factory, client_factory):
        """The whole point of `csv.writer` doing the escaping (rather
        than hand-formatting the row) -- an outcome JSON string
        containing a comma and a double quote must come back byte-for-
        byte identical after a real CSV parse, not truncated or
        misaligned into the wrong column."""
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.VIDEO_REJECT_AND_PURGE, target_type=ModerationTargetType.STUDENT_VIDEO,
                target_id=uuid.uuid4(),
                outcome={"reason": 'Contains, a comma and a "quoted" phrase'},
                created_at="2026-06-01 00:00:00",
            )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-12-31T00:00:00")
        rows = _parse_csv(response.content)
        assert len(rows) == 2
        assert json.loads(rows[1][7]) == {"reason": 'Contains, a comma and a "quoted" phrase'}


class TestDateRangeFiltering:
    async def test_explicit_start_end_excludes_out_of_range_rows(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=uuid.uuid4(), outcome={"tag": "old"}, created_at="2020-01-01 00:00:00",
            )
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=uuid.uuid4(), outcome={"tag": "in_range"}, created_at="2026-06-01 00:00:00",
            )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-12-31T00:00:00")
        rows = _parse_csv(response.content)
        assert len(rows) == 2
        assert json.loads(rows[1][7]) == {"tag": "in_range"}

    async def test_default_range_applied_when_start_and_end_omitted(self, db_session_factory, client_factory, monkeypatch):
        """No `start`/`end` at all -- DEFAULT_EXPORT_RANGE_DAYS is applied
        ending "now", not an unbounded export of the whole table. Since
        the seeded rows must be relative to real "now" for this to be a
        genuine, non-flaky check, this pins `DEFAULT_EXPORT_RANGE_DAYS`
        to a small, known value rather than seeding 90 real days of
        fixtures."""
        monkeypatch.setattr(export_module, "DEFAULT_EXPORT_RANGE_DAYS", 2)
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            # Well outside the last 2 days -- must be excluded by the default.
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=uuid.uuid4(), outcome={"tag": "too_old_for_default"}, created_at="2020-01-01 00:00:00",
            )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export")
        assert response.status_code == 200
        rows = _parse_csv(response.content)
        assert len(rows) == 1  # header only -- the old row falls outside the default range

    async def test_end_before_start_is_422(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-06-01T00:00:00&end=2026-01-01T00:00:00")
        assert response.status_code == 422

    async def test_range_exceeding_max_days_is_422(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
        client = client_factory(owner)
        response = client.get(
            f"/api/v1/admin/activity-log/export?start=2020-01-01T00:00:00&end=2026-01-01T00:00:00"
        )
        assert response.status_code == 422
        assert "exceeds the" in response.json()["detail"]
        assert str(export_module.MAX_EXPORT_RANGE_DAYS) in response.json()["detail"]


class TestRowLimit:
    async def test_row_count_exceeding_max_is_422_not_a_truncated_download(self, db_session_factory, client_factory, monkeypatch):
        """Pins `MAX_EXPORT_ROWS` to a small, known value so this proves
        the real limit logic without seeding 10,001 real rows. A silent
        truncation would be worse than a clear 422 -- the whole point of
        this limit is that the caller finds out their export doesn't fit,
        not that they download a file that looks complete but silently
        drops rows."""
        monkeypatch.setattr(export_module, "MAX_EXPORT_ROWS", 3)
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            for i in range(4):
                await _make_log_entry(
                    session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                    action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                    target_id=uuid.uuid4(), outcome={"seq": i}, created_at=f"2026-06-01 00:00:{i:02d}",
                )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-12-31T00:00:00")
        assert response.status_code == 422
        assert "4 entries match" in response.json()["detail"]
        assert "3-row maximum" in response.json()["detail"]

    async def test_row_count_at_exactly_the_max_succeeds(self, db_session_factory, client_factory, monkeypatch):
        monkeypatch.setattr(export_module, "MAX_EXPORT_ROWS", 3)
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            for i in range(3):
                await _make_log_entry(
                    session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                    action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                    target_id=uuid.uuid4(), outcome={"seq": i}, created_at=f"2026-06-01 00:00:{i:02d}",
                )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-12-31T00:00:00")
        assert response.status_code == 200
        rows = _parse_csv(response.content)
        assert len(rows) == 4  # header + 3


class TestSelfLogging:
    async def test_export_itself_writes_a_real_log_entry(self, db_session_factory, client_factory):
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-02-01T00:00:00")
        assert response.status_code == 200

        async with db_session_factory() as session:
            result = await session.execute(
                text("SELECT actor_id, action, target_type, target_id, outcome FROM moderation_log_entries")
            )
            rows = result.all()
        assert len(rows) == 1  # the export produced zero real log rows to export, but DID log itself
        actor_id, action, target_type, target_id, outcome = rows[0]
        assert actor_id == owner.id.hex
        assert action == "activity_log_export"
        assert target_type == "user"
        assert target_id == owner.id.hex  # self-target, per this action's own docstring
        outcome_data = json.loads(outcome)
        assert outcome_data["row_count"] == 0
        assert outcome_data["format"] == "csv"
        assert outcome_data["start"].startswith("2026-01-01")
        assert outcome_data["end"].startswith("2026-02-01")

    async def test_exported_rows_do_not_include_the_exports_own_not_yet_committed_entry(self, db_session_factory, client_factory):
        """The export's own self-log entry must not appear as a row
        WITHIN the CSV it produces -- it's written and committed only
        after the export query already ran, so this is really just
        confirming that ordering holds, not a special-cased filter."""
        async with db_session_factory() as session:
            owner = await _make_user(session, is_superuser=True, admin_role=AdminRole.OWNER)
            moderator = await _make_user(session, admin_role=AdminRole.MODERATOR)
            await _make_log_entry(
                session, actor_id=moderator.id, actor_role=AdminRole.MODERATOR,
                action=ModerationAction.CHAT_UNBAN, target_type=ModerationTargetType.USER,
                target_id=uuid.uuid4(), outcome={"tag": "real"}, created_at="2026-01-15 00:00:00",
            )

        client = client_factory(owner)
        response = client.get("/api/v1/admin/activity-log/export?start=2026-01-01T00:00:00&end=2026-02-01T00:00:00")
        rows = _parse_csv(response.content)
        assert len(rows) == 2  # header + the one real pre-existing entry only
        assert "activity_log_export" not in [r[4] for r in rows[1:]]
