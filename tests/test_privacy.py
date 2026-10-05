"""APEX Privacy + Social Identity Layer test matrix (Sections 32/33 of
this phase's own spec): relationship resolution, per-category field
filtering for both `GET /users/search` and `GET /users/{user_id}/profile`,
sensitive-field absence, and privacy-settings authorization.

Same "minimal raw-SQL SQLite tables" fixture convention as
`test_user_intelligence.py`/`test_user_search.py` -- this project has no
live Postgres in its test sandbox (disclosed, standing limitation; see
`test_user_search.py`'s own module docstring), so `User.search_vector`'s
`@@`/`websearch_to_tsquery` full-text layer is exercised only at the
query-construction level in `test_user_search.py`, never executed here.
Everything in this file goes through `privacy_service`/`social_service`
directly, or through `TestClient` with the DB dependency overridden to
this in-memory SQLite engine and `CurrentUser` overridden per-test --
none of it needs Postgres-only SQL to run.
"""

from __future__ import annotations

import datetime as dt
import uuid

import pytest
import pytest_asyncio
from fastapi.testclient import TestClient
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import StaticPool

from app.api.deps import get_current_user, get_db
from app.main import app
from app.models.social import UserConnection
from app.models.user import User
from app.models.user_privacy import UserPrivacySettings
from app.schemas.privacy import PublicSearchProfileOut
from app.services import privacy_service, social_service

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
    remote_preference BOOLEAN, funding_preference VARCHAR(30), deadline_lead_time_days INTEGER, date_of_birth DATE, english_level VARCHAR(20), other_nationalities TEXT, onboarding_completed_at DATETIME,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_CONNECTIONS_SQL = """
CREATE TABLE user_connections (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, friend_id VARCHAR(36) NOT NULL,
    status VARCHAR(50) NOT NULL DEFAULT 'pending',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

_CREATE_USER_PRIVACY_SETTINGS_SQL = """
CREATE TABLE user_privacy_settings (
    user_id VARCHAR(36) PRIMARY KEY,
    bio_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    education_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    skills_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    location_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    languages_visibility VARCHAR(20) NOT NULL DEFAULT 'public',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""

# `User.bookmarks` is `lazy="selectin"` (app/models/user.py) -- EVERY
# load of a `User` row through the ORM (not just an explicit `.bookmarks`
# access) issues an automatic follow-up SELECT against this table. Any
# test that loads a `User` via a real query (not just constructs one in
# memory) needs this table to exist, even if the test itself never
# touches bookmarks -- confirmed the hard way via
# `TestAdversarial::test_pending_requests_endpoint_never_returns_
# another_users_requests`, which failed with "no such table:
# user_bookmarks" before this was added.
_CREATE_USER_BOOKMARKS_SQL = """
CREATE TABLE user_bookmarks (
    id VARCHAR(36) PRIMARY KEY, user_id VARCHAR(36) NOT NULL, opportunity_id VARCHAR(36),
    course_id VARCHAR(36), status VARCHAR(20) NOT NULL DEFAULT 'saved',
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP
)
"""


@pytest_asyncio.fixture
async def db_session():
    engine = create_async_engine("sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    async with engine.begin() as conn:
        await conn.execute(text(_CREATE_USERS_SQL))
        await conn.execute(text(_CREATE_USER_CONNECTIONS_SQL))
        await conn.execute(text(_CREATE_USER_PRIVACY_SETTINGS_SQL))
        await conn.execute(text(_CREATE_USER_BOOKMARKS_SQL))

    session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with session_factory() as session:
        yield session
    await engine.dispose()


def _make_user(db, **overrides) -> User:
    """`skills`/`languages`/etc. are Postgres-only `ARRAY(String)`
    columns (see `app/models/user.py`) with no SQLite bind support at
    all -- disclosed, standing sandbox limitation, same one
    `test_user_search.py`'s own module docstring already documents for
    the full-text `@@` operator. Left unset (None) by default so tests
    that insert through the real ORM into the in-memory SQLite engine
    (`TestRelationshipResolution`, `TestPrivacySettingsAuthorization`)
    never touch that column at bind time; `TestFieldFiltering` (which
    never calls `db_session.add` -- pure Python object construction) is
    the only place `skills`/`languages` are actually exercised."""
    defaults = dict(
        id=uuid.uuid4(), email=f"{uuid.uuid4().hex}@example.com", full_name="Test User",
        role="student", bio="hello", major="Physics", degree_level="Bachelor's",
        academic_level="university_year_1", graduation_year=2027,
        target_country="Canada", nationality="Egyptian", residency_country="Egypt",
    )
    defaults.update(overrides)
    return User(**defaults)


class TestRelationshipResolution:
    """SELF / CONNECTION / STRANGER server-side resolution -- reused by
    both /users/search and /users/{id}/profile, never client-supplied."""

    @pytest.mark.asyncio
    async def test_self_is_self(self, db_session):
        user = _make_user(db_session)
        db_session.add(user)
        await db_session.commit()

        relationship = await social_service.get_relationship(db_session, user.id, user.id)
        assert relationship == "self"

    @pytest.mark.asyncio
    async def test_accepted_connection_is_connection(self, db_session):
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="accepted"))
        await db_session.commit()

        relationship = await social_service.get_relationship(db_session, b.id, a.id)
        assert relationship == "connection"

    @pytest.mark.asyncio
    async def test_pending_connection_is_still_stranger(self, db_session):
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="pending"))
        await db_session.commit()

        relationship = await social_service.get_relationship(db_session, b.id, a.id)
        assert relationship == "stranger"

    @pytest.mark.asyncio
    async def test_unrelated_users_are_strangers(self, db_session):
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.commit()

        relationship = await social_service.get_relationship(db_session, a.id, b.id)
        assert relationship == "stranger"


class TestConnectionStateResolution:
    """NETWORK 2.0: the finer-grained, UI-only `connection_state`
    classification (self/connected/request_sent/request_received/none)
    -- a SEPARATE classification from `Relationship` above, resolved
    from the exact same `UserConnection` rows (see
    `social_service.get_connection_state`'s docstring). Mirrors
    `TestRelationshipResolution`'s shape 1:1 since both are resolved
    from the same table, just classified differently."""

    @pytest.mark.asyncio
    async def test_self_is_self(self, db_session):
        user = _make_user(db_session)
        db_session.add(user)
        await db_session.commit()

        state = await social_service.get_connection_state(db_session, user.id, user.id)
        assert state == "self"

    @pytest.mark.asyncio
    async def test_accepted_connection_is_connected_for_both_parties(self, db_session):
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="accepted"))
        await db_session.commit()

        assert await social_service.get_connection_state(db_session, a.id, b.id) == "connected"
        assert await social_service.get_connection_state(db_session, b.id, a.id) == "connected"

    @pytest.mark.asyncio
    async def test_pending_outgoing_request_is_request_sent_for_the_sender(self, db_session):
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="pending"))
        await db_session.commit()

        assert await social_service.get_connection_state(db_session, a.id, b.id) == "request_sent"

    @pytest.mark.asyncio
    async def test_pending_incoming_request_is_request_received_for_the_recipient(self, db_session):
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="pending"))
        await db_session.commit()

        # Same pending row, viewed from the recipient's side -- must be
        # distinguishable from the sender's "request_sent" above, which
        # `Relationship` (collapsed to "stranger" for both) cannot do.
        assert await social_service.get_connection_state(db_session, b.id, a.id) == "request_received"

    @pytest.mark.asyncio
    async def test_unrelated_users_are_no_connection(self, db_session):
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.commit()

        assert await social_service.get_connection_state(db_session, a.id, b.id) == "none"

    @pytest.mark.asyncio
    async def test_declined_connection_is_no_connection_not_a_permanent_state(self, db_session):
        """A `declined` edge carries no live UI state in either
        direction -- see `request_friend`'s lifecycle-fix docstring:
        declined must be re-requestable, so it must not render as a
        stuck/blocked state on either side either."""
        a, b = _make_user(db_session), _make_user(db_session)
        db_session.add_all([a, b])
        await db_session.flush()
        db_session.add(UserConnection(id=uuid.uuid4(), user_id=a.id, friend_id=b.id, status="declined"))
        await db_session.commit()

        assert await social_service.get_connection_state(db_session, a.id, b.id) == "none"
        assert await social_service.get_connection_state(db_session, b.id, a.id) == "none"

    @pytest.mark.asyncio
    async def test_bulk_resolution_matches_single_resolution_for_every_state(self, db_session):
        """`get_connection_states_bulk` must classify every target
        identically to calling `get_connection_state` once per target --
        the whole point of the bulk path is to be a pure performance
        optimization, never a behavior change."""
        viewer = _make_user(db_session)
        connected = _make_user(db_session)
        sent_to = _make_user(db_session)
        received_from = _make_user(db_session)
        stranger = _make_user(db_session)
        db_session.add_all([viewer, connected, sent_to, received_from, stranger])
        await db_session.flush()
        db_session.add_all([
            UserConnection(id=uuid.uuid4(), user_id=viewer.id, friend_id=connected.id, status="accepted"),
            UserConnection(id=uuid.uuid4(), user_id=viewer.id, friend_id=sent_to.id, status="pending"),
            UserConnection(id=uuid.uuid4(), user_id=received_from.id, friend_id=viewer.id, status="pending"),
        ])
        await db_session.commit()

        targets = [viewer.id, connected.id, sent_to.id, received_from.id, stranger.id]
        bulk = await social_service.get_connection_states_bulk(db_session, viewer.id, targets)

        assert bulk[viewer.id] == "self"
        assert bulk[connected.id] == "connected"
        assert bulk[sent_to.id] == "request_sent"
        assert bulk[received_from.id] == "request_received"
        assert bulk[stranger.id] == "none"

        for target_id in targets:
            single = await social_service.get_connection_state(db_session, viewer.id, target_id)
            assert single == bulk[target_id]


class TestFieldFiltering:
    """PUBLIC FIELD visible when allowed / PRIVATE FIELD never leaks --
    both /users/search's and /users/{id}/profile's shared filtering
    logic, exercised directly against `privacy_service`."""

    def test_no_settings_row_defaults_to_public_not_a_regression(self):
        target = _make_user(None, skills=["Python", "Research"])
        row = privacy_service.filter_search_row(target, None, "stranger")
        assert row.bio == "hello"
        assert row.major == "Physics"
        assert row.skills == ["Python", "Research"]

    def test_private_field_hidden_from_stranger(self):
        target = _make_user(None)
        settings = UserPrivacySettings(user_id=target.id, bio_visibility="private")
        row = privacy_service.filter_search_row(target, settings, "stranger")
        assert row.bio is None
        assert row.major == "Physics"  # education_visibility untouched, still public

    def test_connections_only_field_hidden_from_stranger_shown_to_connection(self):
        target = _make_user(None)
        settings = UserPrivacySettings(user_id=target.id, education_visibility="connections")
        stranger_row = privacy_service.filter_search_row(target, settings, "stranger")
        connection_row = privacy_service.filter_search_row(target, settings, "connection")
        assert stranger_row.major is None
        assert stranger_row.degree_level is None
        assert connection_row.major == "Physics"

    def test_self_always_sees_everything_regardless_of_settings(self):
        target = _make_user(None, skills=["Python", "Research"])
        settings = UserPrivacySettings(
            user_id=target.id, bio_visibility="private", education_visibility="private",
            skills_visibility="private", location_visibility="private", languages_visibility="private",
        )
        row = privacy_service.filter_search_row(target, settings, "self")
        assert row.bio == "hello"
        assert row.major == "Physics"
        assert row.skills == ["Python", "Research"]

    def test_sensitive_fields_are_not_even_a_field_on_the_search_schema(self):
        """The actual vulnerability this phase fixes: `GET /users/search`
        used to return full `UserOut` (email/phone/gpa/gender/is_active/
        created_at) to any signed-in user. `PublicSearchProfileOut` must
        not have ANY of those fields -- not nulled out, structurally
        absent -- so there is no code path that can ever populate them."""
        field_names = set(PublicSearchProfileOut.model_fields.keys())
        for sensitive in ("email", "phone", "gpa", "gender", "is_active", "created_at"):
            assert sensitive not in field_names

    def test_profile_view_never_exposes_personal_intelligence_fields(self):
        """PERSONAL INTELLIGENCE (interests/application_goals/funding_
        preference/etc.) stays self-only, always -- `social visibility
        != personalization`. Not on `ProfileViewOut` at all, for any
        relationship, since APEX must keep using these for the target's
        OWN recommendations even if every other field is locked down."""
        from app.schemas.privacy import ProfileViewOut

        field_names = set(ProfileViewOut.model_fields.keys())
        for personal in (
            "interests", "application_goals", "preferred_opportunity_types",
            "funding_preference", "remote_preference", "deadline_lead_time_days",
            "email", "phone", "gpa", "gender",
        ):
            assert personal not in field_names


    def test_connection_state_never_changes_field_visibility(self):
        """Gate D (resume directive): `connection_state` is a SEPARATE,
        additive classification (see `privacy_service.filter_search_
        row`'s own docstring) that must never participate in field-level
        privacy filtering. A `request_sent`/`request_received` pending
        request is still, for privacy purposes, a "stranger" -- it must
        see exactly what a `connection_state="none"` stranger sees, not
        one field more, regardless of which `connection_state` string is
        passed through."""
        target = _make_user(None)
        settings = UserPrivacySettings(user_id=target.id, bio_visibility="private", education_visibility="connections")

        baseline = privacy_service.filter_search_row(target, settings, "stranger", "none")
        for state in ("request_sent", "request_received"):
            row = privacy_service.filter_search_row(target, settings, "stranger", state)
            assert row.bio == baseline.bio == None
            assert row.major == baseline.major == None
            assert row.connection_state == state  # the field itself DOES reflect the passed value

        # Only an actual `relationship` upgrade (not `connection_state`)
        # ever unlocks a "connections"-visibility field.
        connection_row = privacy_service.filter_search_row(target, settings, "connection", "request_sent")
        assert connection_row.major == "Physics"


class TestPrivacySettingsAuthorization:
    """PRIVACY UPDATE persists / unauthorized-user-cannot-change-another-
    user's-settings -- exercised through the real router with `DbSession`
    and `CurrentUser` overridden, same pattern `test_user_search.py`'s
    `TestUserSearchRouterValidation` already establishes for this
    codebase."""

    @pytest_asyncio.fixture
    async def client(self, db_session):
        user_a = _make_user(db_session, id=uuid.uuid4())
        user_b = _make_user(db_session, id=uuid.uuid4())
        db_session.add_all([user_a, user_b])
        await db_session.commit()

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        app.dependency_overrides[get_current_user] = lambda: user_a
        yield TestClient(app), user_a, user_b
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    def test_get_creates_and_returns_default_public_settings(self, client):
        ac, user_a, _ = client
        resp = ac.get("/api/v1/users/me/privacy-settings")
        assert resp.status_code == 200
        body = resp.json()
        assert body["bio_visibility"] == "public"
        assert body["education_visibility"] == "public"

    def test_patch_persists_only_for_caller(self, client):
        ac, user_a, user_b = client
        resp = ac.patch(
            "/api/v1/users/me/privacy-settings", json={"bio_visibility": "private"}
        )
        assert resp.status_code == 200
        assert resp.json()["bio_visibility"] == "private"

        # Re-reading confirms the write landed on user_a's own row.
        follow_up = ac.get("/api/v1/users/me/privacy-settings")
        assert follow_up.json()["bio_visibility"] == "private"

    @pytest.mark.asyncio
    async def test_patch_request_body_has_no_user_id_field(self, client):
        """Structural authorization check (Rule 3): the update schema
        offers no way to name a target user at all -- the row modified
        is always derived from the authenticated session, never from
        caller-supplied input, so there is no IDOR surface here to test
        against in the first place."""
        from app.schemas.privacy import UserPrivacySettingsUpdate

        assert "user_id" not in UserPrivacySettingsUpdate.model_fields


class TestAdversarial:
    """Section 18 of the Master Hardening directive's adversarial matrix,
    scoped to the endpoints this phase actually added/changed --
    `GET /users/{user_id}/profile`, `GET /social/friends/requests/
    pending`, and `GET/PATCH /users/me/privacy-settings`. Chat and the
    pre-existing `respond_to_friend_request`/`request_friend` IDOR
    guards were audited (not modified) in the prior pass; this class
    proves the NEW surface, not re-proves the old one."""

    @pytest_asyncio.fixture
    async def client(self, db_session):
        user_a = _make_user(db_session, id=uuid.uuid4(), full_name="User A", bio="A's private bio")
        user_b = _make_user(db_session, id=uuid.uuid4(), full_name="User B", bio="B's private bio")
        db_session.add_all([user_a, user_b])
        await db_session.commit()

        async def _override_db():
            yield db_session

        app.dependency_overrides[get_db] = _override_db
        yield TestClient(app), db_session, user_a, user_b
        app.dependency_overrides.pop(get_db, None)
        app.dependency_overrides.pop(get_current_user, None)

    def _as(self, user):
        app.dependency_overrides[get_current_user] = lambda: user

    def test_anonymous_cannot_view_any_profile(self, client):
        """A. Anonymous -> profile endpoint. No `get_current_user`
        override at all -- a genuinely unauthenticated request."""
        ac, _, _, user_b = client
        resp = ac.get(f"/api/v1/users/{user_b.id}/profile")
        assert resp.status_code in (401, 403)

    def test_stranger_gets_filtered_profile_not_raw_data(self, client):
        """C. Stranger -> private field, via the real HTTP route (not
        just the unit-level `privacy_service` call already covered in
        `TestFieldFiltering`) -- proves the router actually wires the
        relationship + settings lookup end to end."""
        ac, db, user_a, user_b = client
        db.add(UserPrivacySettings(user_id=user_b.id, bio_visibility="private"))
        import asyncio
        asyncio.get_event_loop().run_until_complete(db.commit())
        self._as(user_a)
        resp = ac.get(f"/api/v1/users/{user_b.id}/profile")
        assert resp.status_code == 200
        body = resp.json()
        assert body["bio"] is None
        assert body["relationship"] == "stranger"

    def test_self_viewing_own_profile_sees_everything(self, client):
        ac, _, user_a, _ = client
        self._as(user_a)
        resp = ac.get(f"/api/v1/users/{user_a.id}/profile")
        assert resp.status_code == 200
        assert resp.json()["relationship"] == "self"
        assert resp.json()["bio"] == "A's private bio"

    def test_malformed_user_id_is_rejected_not_500(self, client):
        """T. Malformed ID -- FastAPI's own UUID path-param validation
        should reject this with a 422 before any handler code runs, not
        surface as an unhandled 500."""
        ac, _, user_a, _ = client
        self._as(user_a)
        resp = ac.get("/api/v1/users/not-a-real-uuid/profile")
        assert resp.status_code == 422

    def test_nonexistent_user_id_is_404_not_leaked_as_500_or_200(self, client):
        """U. A syntactically valid but nonexistent user id (stand-in
        for a deleted user) must be a clean 404, not a crash and not a
        200 with fabricated data."""
        ac, _, user_a, _ = client
        self._as(user_a)
        resp = ac.get(f"/api/v1/users/{uuid.uuid4()}/profile")
        assert resp.status_code == 404

    def test_pending_requests_endpoint_never_returns_another_users_requests(self, client):
        """J/O -- `GET /social/friends/requests/pending` must be scoped
        to `current_user.id` alone, never to a caller-supplied id (there
        is no id parameter on this endpoint at all, but this proves the
        query itself, not just the route signature): user_a has no
        incoming requests, even though user_b does (from a third user)."""
        ac, db, user_a, user_b = client
        third = _make_user(db, id=uuid.uuid4(), full_name="User C")
        db.add(third)
        db.add(UserConnection(id=uuid.uuid4(), user_id=third.id, friend_id=user_b.id, status="pending"))
        import asyncio
        asyncio.get_event_loop().run_until_complete(db.commit())

        self._as(user_a)
        resp = ac.get("/api/v1/social/friends/requests/pending")
        assert resp.status_code == 200
        assert resp.json() == []

        self._as(user_b)
        resp = ac.get("/api/v1/social/friends/requests/pending")
        assert resp.status_code == 200
        assert len(resp.json()) == 1
        assert resp.json()[0]["from_user_id"] == str(third.id)

    def test_privacy_settings_endpoints_require_authentication(self, client):
        """A. Anonymous -> privacy settings (no override at all)."""
        ac, _, _, _ = client
        assert ac.get("/api/v1/users/me/privacy-settings").status_code in (401, 403)
        assert ac.patch("/api/v1/users/me/privacy-settings", json={"bio_visibility": "private"}).status_code in (401, 403)


class TestSearchPerformance:
    """Final Hardening Gate, Sections 3/4/25: proves the real N+1 this
    phase's own prior audit found in `GET /users/search` (one
    `_get_friend_ids` query PER result row, via a per-row call to
    `social_service.get_relationship`) is actually gone, by counting
    real SQL statements executed -- not asserting on code shape.

    `GET /users/search` itself can't be exercised end-to-end here (its
    `websearch_to_tsquery`/`@@` full-text query is genuinely
    Postgres-only -- see this file's own module docstring and `test_
    user_search.py`), so this targets `social_service.get_relationships_
    bulk` directly: that is exactly the function the router now calls
    once per request instead of calling `get_relationship` once per
    result row, and it is where the fix actually lives.
    """

    @pytest_asyncio.fixture
    async def engine_and_session(self):
        engine = create_async_engine(
            "sqlite+aiosqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False}
        )
        async with engine.begin() as conn:
            await conn.execute(text(_CREATE_USERS_SQL))
            await conn.execute(text(_CREATE_USER_CONNECTIONS_SQL))

        session_factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
        async with session_factory() as session:
            yield engine, session
        await engine.dispose()

    def _count_queries(self, engine):
        count = {"n": 0}

        def _on_execute(*args, **kwargs):
            count["n"] += 1

        from sqlalchemy import event

        event.listen(engine.sync_engine, "before_cursor_execute", _on_execute)
        return count

    @pytest.mark.asyncio
    async def test_bulk_relationship_resolution_issues_one_query_regardless_of_result_count(
        self, engine_and_session
    ):
        engine, db = engine_and_session
        viewer = _make_user(db, id=uuid.uuid4())
        targets_5 = [_make_user(db, id=uuid.uuid4()) for _ in range(5)]
        targets_20 = [_make_user(db, id=uuid.uuid4()) for _ in range(20)]
        db.add(viewer)
        db.add_all(targets_5)
        db.add_all(targets_20)
        await db.commit()

        counter = self._count_queries(engine)
        counter["n"] = 0
        await social_service.get_relationships_bulk(db, viewer.id, [t.id for t in targets_5])
        queries_for_5 = counter["n"]

        counter["n"] = 0
        await social_service.get_relationships_bulk(db, viewer.id, [t.id for t in targets_20])
        queries_for_20 = counter["n"]

        # The whole point of the fix: query count does NOT grow with
        # the number of targets. Bounded (O(1)), not "exactly N".
        assert queries_for_5 == 1
        assert queries_for_20 == 1
        assert queries_for_5 == queries_for_20

    @pytest.mark.asyncio
    async def test_old_per_row_pattern_would_have_been_on_result_count(self, engine_and_session):
        """Negative control, proving the counter itself is meaningful:
        the OLD pattern this phase replaced (`get_relationship` called
        once per row) really does scale with result count -- confirming
        `test_bulk_...` above is measuring a real fix, not a test that
        would have passed either way."""
        engine, db = engine_and_session
        viewer = _make_user(db, id=uuid.uuid4())
        targets = [_make_user(db, id=uuid.uuid4()) for _ in range(5)]
        db.add(viewer)
        db.add_all(targets)
        await db.commit()

        counter = self._count_queries(engine)
        counter["n"] = 0
        for target in targets:
            await social_service.get_relationship(db, viewer.id, target.id)
        assert counter["n"] == len(targets)  # confirms O(N) is what "the old way" actually does
