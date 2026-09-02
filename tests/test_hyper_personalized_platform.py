"""Behavioral tests for the Hyper-Personalized Platform step:

- `UserBookmark` polymorphism (`opportunity_id` OR `course_id`, exactly
  one) -- `app/models/bookmark.py`.
- Three new models -- `app/models/features.py`:
  `Certificate`, `LessonStudyAid`, `MentorshipRelation`.
- `/mentorship` router -- `app/api/v1/endpoints/mentorship.py`.
- `GET /ai/cv-builder` -- `app/api/v1/endpoints/ai_cv_builder.py`.

In-memory SQLite, `StaticPool`, same established discipline as `tests/
test_dashboards.py`/`tests/test_academic_level.py`:

- `users` -- raw SQL (same shape as every other test file's own copy).
- `user_bookmarks`/`certificates`/`mentorship_relations` -- created via
  `Base.metadata.create_all(tables=[...])` directly off the REAL ORM
  metadata: none of their columns are JSONB/ARRAY/Vector, so SQLAlchemy's
  generic DDL compiler handles them (including their `CheckConstraint`s
  and `UniqueConstraint`s -- SQLite enforces both natively) with no
  SQLite-incompatibility workarounds needed, unlike `opportunities`
  elsewhere in this codebase.
- `lesson_study_aids` -- raw SQL instead (its `flashcards` column IS a
  `postgresql.JSONB`, which has no generic SQLite DDL compiler support --
  same class of gap `tests/test_aggregator.py`'s own docstring already
  establishes for `Opportunity.raw_extracted_data`/`embedding`).
- `courses`/`user_enrollments` -- raw SQL (`Course.embedding` is a
  pgvector column, same reasoning).
- `opportunities` -- raw SQL, same shape as `tests/test_dashboards.py`'s
  own copy. DISCLOSED, NECESSARY ADDITION found while writing this file
  (not assumed up front): `UserBookmark.opportunity` is `lazy=
  "selectin"` (unchanged by this step), so even a COURSE-typed bookmark
  row (`opportunity_id IS NULL`) still issues a `SELECT ... FROM
  opportunities WHERE id = ?` the moment the row is refreshed/loaded --
  the table must exist even though no row in it is ever populated by
  these tests.
- `student_videos` -- raw SQL, minimal columns (only what `ai_cv_builder.py`
  actually reads: `id`, `student_id`, `title`, `status`, `ai_score`,
  `created_at`).

SQLite does NOT enforce `FOREIGN KEY` constraints unless a session-level
PRAGMA turns it on (not done here, matching every other test file in this
codebase) -- so parent rows (`users`/`courses`) are still created for
realism and for `db.get(User, ...)`/join queries to have something real
to find, but a dangling FK value would NOT itself raise here the way it
would against real Postgres.
"""

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user
from app.db.base import Base
from app.db.session import get_db
from app.main import app
from app.models.badges import Badge, GamificationProfile, UserBadge
from app.models.bookmark import UserBookmark
from app.models.enums import MentorshipStatus
from app.models.features import Certificate, MentorshipRelation
from app.models.user import User

pytestmark = pytest.mark.asyncio

# ---------------------------------------------------------------------
# Raw-SQL tables
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
    role_granted_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_COURSES_TABLE_SQL = """
CREATE TABLE courses (
    id VARCHAR(36) PRIMARY KEY,
    title VARCHAR(255) NOT NULL,
    description TEXT,
    instructor_name VARCHAR(255) NOT NULL DEFAULT 'AI Scholar Guide',
    token_unlock_cost INTEGER NOT NULL DEFAULT 0,
    thumbnail_url VARCHAR(1000),
    thumbnail_cid VARCHAR(255),
    embedding TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_ENROLLMENTS_TABLE_SQL = """
CREATE TABLE user_enrollments (
    user_id VARCHAR(36) NOT NULL,
    course_id VARCHAR(36) NOT NULL,
    enrolled_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id, course_id)
)
"""

_CREATE_OPPORTUNITIES_TABLE_SQL = """
CREATE TABLE opportunities (
    id VARCHAR(36) PRIMARY KEY,
    title VARCHAR(500) NOT NULL,
    description TEXT,
    target_url VARCHAR(2048) NOT NULL,
    source_id VARCHAR(36) NOT NULL,
    type VARCHAR(20) NOT NULL,
    deadline DATETIME,
    eligibility_criteria TEXT,
    funding_details TEXT,
    location VARCHAR(255),
    country VARCHAR(120) DEFAULT 'Global',
    is_verified BOOLEAN NOT NULL DEFAULT 0,
    ai_confidence_score FLOAT,
    raw_extracted_data TEXT,
    embedding TEXT,
    is_local_egypt BOOLEAN NOT NULL DEFAULT 0,
    target_age_group VARCHAR(50),
    target_academic_levels TEXT,
    min_age INTEGER,
    max_age INTEGER,
    target_gender VARCHAR(10),
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    review_status VARCHAR(20) NOT NULL DEFAULT 'published',
    claimed_by VARCHAR(36),
    claimed_at DATETIME
)
"""

_CREATE_LESSON_STUDY_AIDS_TABLE_SQL = """
CREATE TABLE lesson_study_aids (
    id VARCHAR(36) PRIMARY KEY,
    lesson_id VARCHAR(36) UNIQUE,
    student_video_id VARCHAR(36) UNIQUE,
    summary_text TEXT,
    flashcards TEXT,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (lesson_id IS NOT NULL AND student_video_id IS NULL)
        OR (lesson_id IS NULL AND student_video_id IS NOT NULL)
    )
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
        await conn.execute(text(_CREATE_COURSES_TABLE_SQL))
        await conn.execute(text(_CREATE_OPPORTUNITIES_TABLE_SQL))
        await conn.execute(text(_CREATE_USER_ENROLLMENTS_TABLE_SQL))
        await conn.execute(text(_CREATE_LESSON_STUDY_AIDS_TABLE_SQL))
        await conn.execute(text(_CREATE_STUDENT_VIDEOS_TABLE_SQL))
        await conn.run_sync(
            Base.metadata.create_all,
            tables=[
                UserBookmark.__table__,
                Certificate.__table__,
                MentorshipRelation.__table__,
                GamificationProfile.__table__,
                Badge.__table__,
                UserBadge.__table__,
            ],
        )

    session_factory = async_sessionmaker(bind=engine, class_=AsyncSession, expire_on_commit=False)
    yield session_factory
    await engine.dispose()


async def _make_user(db: AsyncSession, *, is_superuser: bool = False, full_name: str = "Test User") -> User:
    user = User(id=uuid.uuid4(), email=f"{uuid.uuid4()}@example.com", full_name=full_name, is_superuser=is_superuser)
    db.add(user)
    await db.commit()
    await db.refresh(user)
    return user


async def _make_course(db: AsyncSession, *, title: str = "Intro to Everything") -> uuid.UUID:
    course_id = uuid.uuid4()
    await db.execute(
        text("INSERT INTO courses (id, title) VALUES (:id, :title)"),
        {"id": course_id.hex, "title": title},
    )
    await db.commit()
    return course_id


async def _enroll(db: AsyncSession, *, user_id: uuid.UUID, course_id: uuid.UUID) -> None:
    await db.execute(
        text("INSERT INTO user_enrollments (user_id, course_id) VALUES (:user_id, :course_id)"),
        {"user_id": user_id.hex, "course_id": course_id.hex},
    )
    await db.commit()


async def _make_video(db: AsyncSession, *, student_id: uuid.UUID, status: str, ai_score: int | None = None) -> uuid.UUID:
    video_id = uuid.uuid4()
    await db.execute(
        text(
            "INSERT INTO student_videos (id, student_id, title, status, ai_score) "
            "VALUES (:id, :student_id, :title, :status, :ai_score)"
        ),
        {"id": video_id.hex, "student_id": student_id.hex, "title": "My Project", "status": status, "ai_score": ai_score},
    )
    await db.commit()
    return video_id


@pytest.fixture
def client_factory(db_session_factory):
    """Same `(TestClient, overrides-cleared-on-teardown)` technique as
    `tests/test_dashboards.py`'s own `client_factory`."""

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
# UserBookmark polymorphism
# ---------------------------------------------------------------------


class TestBookmarkPolymorphism:
    async def test_course_only_bookmark_persists(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course_id = await _make_course(db)

            bookmark = UserBookmark(user_id=user.id, course_id=course_id, opportunity_id=None)
            db.add(bookmark)
            await db.commit()
            await db.refresh(bookmark)

            assert bookmark.course_id == course_id
            assert bookmark.opportunity_id is None

    async def test_opportunity_only_bookmark_still_works_unchanged(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            opportunity_id = uuid.uuid4()

            bookmark = UserBookmark(user_id=user.id, opportunity_id=opportunity_id, course_id=None)
            db.add(bookmark)
            await db.commit()
            await db.refresh(bookmark)

            assert bookmark.opportunity_id == opportunity_id
            assert bookmark.course_id is None

    async def test_both_targets_set_violates_check_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course_id = await _make_course(db)

            bookmark = UserBookmark(user_id=user.id, course_id=course_id, opportunity_id=uuid.uuid4())
            db.add(bookmark)
            with pytest.raises(IntegrityError):
                await db.commit()

    async def test_neither_target_set_violates_check_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)

            bookmark = UserBookmark(user_id=user.id, course_id=None, opportunity_id=None)
            db.add(bookmark)
            with pytest.raises(IntegrityError):
                await db.commit()

    async def test_duplicate_user_course_pair_violates_unique_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course_id = await _make_course(db)

            db.add(UserBookmark(user_id=user.id, course_id=course_id, opportunity_id=None))
            await db.commit()

            db.add(UserBookmark(user_id=user.id, course_id=course_id, opportunity_id=None))
            with pytest.raises(IntegrityError):
                await db.commit()


# ---------------------------------------------------------------------
# Certificate
# ---------------------------------------------------------------------


class TestCertificateModel:
    async def test_round_trip(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course_id = await _make_course(db)

            cert = Certificate(user_id=user.id, course_id=course_id, certificate_url="https://cdn.example.com/c.pdf")
            db.add(cert)
            await db.commit()
            await db.refresh(cert)

            assert cert.certificate_url == "https://cdn.example.com/c.pdf"
            assert cert.issued_at is not None

    async def test_duplicate_user_course_pair_violates_unique_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)
            course_id = await _make_course(db)

            db.add(Certificate(user_id=user.id, course_id=course_id))
            await db.commit()

            db.add(Certificate(user_id=user.id, course_id=course_id))
            with pytest.raises(IntegrityError):
                await db.commit()


# ---------------------------------------------------------------------
# LessonStudyAid -- raw SQL (see module docstring: JSONB has no generic
# SQLite DDL compiler support, so the ORM's own `insert()` isn't used
# here, only the schema's CHECK constraint is exercised at the SQL layer).
# ---------------------------------------------------------------------


class TestLessonStudyAidConstraint:
    async def test_lesson_linked_row_inserts(self, db_session_factory):
        async with db_session_factory() as db:
            await db.execute(
                text(
                    "INSERT INTO lesson_study_aids (id, lesson_id, summary_text, flashcards) "
                    "VALUES (:id, :lesson_id, :summary_text, :flashcards)"
                ),
                {"id": uuid.uuid4().hex, "lesson_id": uuid.uuid4().hex, "summary_text": "A summary.", "flashcards": "[]"},
            )
            await db.commit()

    async def test_video_linked_row_inserts(self, db_session_factory):
        async with db_session_factory() as db:
            await db.execute(
                text(
                    "INSERT INTO lesson_study_aids (id, student_video_id, summary_text, flashcards) "
                    "VALUES (:id, :student_video_id, :summary_text, :flashcards)"
                ),
                {"id": uuid.uuid4().hex, "student_video_id": uuid.uuid4().hex, "summary_text": "A summary.", "flashcards": "[]"},
            )
            await db.commit()

    async def test_both_targets_set_violates_check_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            with pytest.raises(IntegrityError):
                await db.execute(
                    text(
                        "INSERT INTO lesson_study_aids (id, lesson_id, student_video_id) "
                        "VALUES (:id, :lesson_id, :student_video_id)"
                    ),
                    {"id": uuid.uuid4().hex, "lesson_id": uuid.uuid4().hex, "student_video_id": uuid.uuid4().hex},
                )
                await db.commit()

    async def test_neither_target_set_violates_check_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            with pytest.raises(IntegrityError):
                await db.execute(
                    text("INSERT INTO lesson_study_aids (id) VALUES (:id)"),
                    {"id": uuid.uuid4().hex},
                )
                await db.commit()


# ---------------------------------------------------------------------
# MentorshipRelation
# ---------------------------------------------------------------------


class TestMentorshipRelationModel:
    async def test_round_trip(self, db_session_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db, full_name="Mentor Person")
            mentee = await _make_user(db, full_name="Mentee Person")

            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)

            assert relation.status == MentorshipStatus.PENDING
            assert relation.responded_at is None

    async def test_self_mentorship_violates_check_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)

            db.add(MentorshipRelation(mentor_id=user.id, mentee_id=user.id))
            with pytest.raises(IntegrityError):
                await db.commit()

    async def test_duplicate_pair_violates_unique_constraint(self, db_session_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)

            db.add(MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id))
            await db.commit()

            db.add(MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id))
            with pytest.raises(IntegrityError):
                await db.commit()


# ---------------------------------------------------------------------
# POST /api/v1/mentorship/request, /respond, /complete, GET /mine
# ---------------------------------------------------------------------


class TestMentorshipRouter:
    async def test_request_creates_pending_relation(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db, full_name="Mentor Person")
            mentee = await _make_user(db, full_name="Mentee Person")

        client = client_factory(mentee)
        response = client.post("/api/v1/mentorship/request", json={"mentor_id": str(mentor.id)})

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "pending"
        assert body["mentor_id"] == str(mentor.id)
        assert body["mentee_id"] == str(mentee.id)
        # From the mentee's (requester's) own point of view, the OTHER
        # party is the mentor.
        assert body["other_party"]["full_name"] == "Mentor Person"

    async def test_cannot_request_yourself(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)

        client = client_factory(user)
        response = client.post("/api/v1/mentorship/request", json={"mentor_id": str(user.id)})

        assert response.status_code == 400

    async def test_duplicate_request_returns_409(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)

        client = client_factory(mentee)
        first = client.post("/api/v1/mentorship/request", json={"mentor_id": str(mentor.id)})
        assert first.status_code == 201

        second = client.post("/api/v1/mentorship/request", json={"mentor_id": str(mentor.id)})
        assert second.status_code == 409

    async def test_only_the_requested_mentor_can_approve(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)
            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)

        # The MENTEE trying to approve their own request -- forbidden.
        client = client_factory(mentee)
        response = client.post(f"/api/v1/mentorship/{relation.id}/respond", json={"action": "approve"})
        assert response.status_code == 403

    async def test_approve_transitions_to_active(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db, full_name="Mentor Person")
            mentee = await _make_user(db, full_name="Mentee Person")
            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)

        client = client_factory(mentor)
        response = client.post(f"/api/v1/mentorship/{relation.id}/respond", json={"action": "approve"})

        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "active"
        assert body["responded_at"] is not None
        # From the mentor's own point of view, the OTHER party is the mentee.
        assert body["other_party"]["full_name"] == "Mentee Person"

    async def test_reject_deletes_the_row(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)
            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)
            relation_id = relation.id

        client = client_factory(mentor)
        response = client.post(f"/api/v1/mentorship/{relation_id}/respond", json={"action": "reject"})
        assert response.status_code == 204

        async with db_session_factory() as db:
            assert await db.get(MentorshipRelation, relation_id) is None

    async def test_respond_when_not_pending_returns_400(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)
            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id, status=MentorshipStatus.ACTIVE)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)

        client = client_factory(mentor)
        response = client.post(f"/api/v1/mentorship/{relation.id}/respond", json={"action": "approve"})
        assert response.status_code == 400

    async def test_complete_by_either_party_transitions_to_completed(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)
            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id, status=MentorshipStatus.ACTIVE)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)

        # The MENTEE (not just the mentor) may mark it complete.
        client = client_factory(mentee)
        response = client.post(f"/api/v1/mentorship/{relation.id}/complete", json={})

        assert response.status_code == 200
        assert response.json()["status"] == "completed"

    async def test_complete_by_a_non_participant_returns_403(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)
            outsider = await _make_user(db)
            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id, status=MentorshipStatus.ACTIVE)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)

        client = client_factory(outsider)
        response = client.post(f"/api/v1/mentorship/{relation.id}/complete", json={})
        assert response.status_code == 403

    async def test_complete_while_still_pending_returns_400(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            mentor = await _make_user(db)
            mentee = await _make_user(db)
            relation = MentorshipRelation(mentor_id=mentor.id, mentee_id=mentee.id)
            db.add(relation)
            await db.commit()
            await db.refresh(relation)

        client = client_factory(mentor)
        response = client.post(f"/api/v1/mentorship/{relation.id}/complete", json={})
        assert response.status_code == 400

    async def test_mine_returns_relations_from_both_directions(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            central = await _make_user(db, full_name="Central Person")
            as_mentor_of = await _make_user(db, full_name="My Mentee")
            as_mentee_of = await _make_user(db, full_name="My Mentor")

            db.add(MentorshipRelation(mentor_id=central.id, mentee_id=as_mentor_of.id))
            db.add(MentorshipRelation(mentor_id=as_mentee_of.id, mentee_id=central.id))
            await db.commit()

        client = client_factory(central)
        response = client.get("/api/v1/mentorship/mine")

        assert response.status_code == 200
        body = response.json()
        assert len(body) == 2
        other_party_names = {entry["other_party"]["full_name"] for entry in body}
        assert other_party_names == {"My Mentee", "My Mentor"}


# ---------------------------------------------------------------------
# GET /api/v1/ai/cv-builder
# ---------------------------------------------------------------------


class TestCVBuilder:
    async def test_new_user_gets_zeroed_sections_not_a_404(self, db_session_factory, client_factory):
        async with db_session_factory() as db:
            user = await _make_user(db)

        client = client_factory(user)
        response = client.get("/api/v1/ai/cv-builder")

        assert response.status_code == 200
        body = response.json()
        assert body["total_xp"] == 0
        assert body["current_streak"] == 0
        assert body["badges"] == []
        assert body["courses"] == []
        assert body["certificates"] == []
        assert body["showcase_videos"] == []

    async def test_aggregates_profile_gamification_badges_courses_certs_and_videos(
        self, db_session_factory, client_factory
    ):
        async with db_session_factory() as db:
            user = await _make_user(db, full_name="Aggregated Student")
            user.major = "Computer Science"
            user.gpa = 3.8
            await db.commit()

            db.add(GamificationProfile(user_id=user.id, total_xp=250, current_streak=4, highest_streak=9))
            badge = Badge(id=uuid.uuid4(), name="Scholar", description="d", required_xp=100, icon_url=None)
            db.add(badge)
            await db.commit()
            db.add(UserBadge(user_id=user.id, badge_id=badge.id))
            await db.commit()

            course_id = await _make_course(db, title="Intro to AI")
            await _enroll(db, user_id=user.id, course_id=course_id)

            db.add(Certificate(user_id=user.id, course_id=course_id, certificate_url="https://cdn.example.com/c.pdf"))
            await db.commit()

            # Only the APPROVED video should show up as a showcase entry.
            await _make_video(db, student_id=user.id, status="approved", ai_score=91)
            await _make_video(db, student_id=user.id, status="pending_ai", ai_score=None)
            await _make_video(db, student_id=user.id, status="rejected", ai_score=20)

        client = client_factory(user)
        response = client.get("/api/v1/ai/cv-builder")

        assert response.status_code == 200
        body = response.json()

        assert body["profile"]["full_name"] == "Aggregated Student"
        assert body["profile"]["major"] == "Computer Science"
        assert body["profile"]["gpa"] == 3.8

        assert body["total_xp"] == 250
        assert body["current_streak"] == 4
        assert body["highest_streak"] == 9

        assert len(body["badges"]) == 1
        assert body["badges"][0]["name"] == "Scholar"

        assert len(body["courses"]) == 1
        assert body["courses"][0]["title"] == "Intro to AI"

        assert len(body["certificates"]) == 1
        assert body["certificates"][0]["course_title"] == "Intro to AI"
        assert body["certificates"][0]["certificate_url"] == "https://cdn.example.com/c.pdf"

        assert len(body["showcase_videos"]) == 1
        assert body["showcase_videos"][0]["ai_score"] == 91
