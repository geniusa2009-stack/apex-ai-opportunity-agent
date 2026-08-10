"""Tests for `GET /api/v1/users/search`
(`app/api/v1/endpoints/users.py::search_users`/`build_user_search_statement`).

DISCLOSED, IMPORTANT SANDBOX LIMITATION -- read before extending this
file: `websearch_to_tsquery`, `ts_rank`, and the `@@` match operator are
genuinely Postgres-only. SQLite (the only database available anywhere in
this project's own test suite -- no live Postgres exists in this sandbox,
same disclosed limitation as every migration's own README entry) raises
`sqlite3.OperationalError: no such function: websearch_to_tsquery` the
instant a statement using any of them is actually EXECUTED against a
real connection. This means the one thing this test file genuinely
CANNOT do is run a real end-to-end "does searching for 'cairo mentor'
actually return the right rows in the right rank order" test -- that
requires a live Postgres this sandbox does not have.

What this file DOES verify, all for real, nothing mocked or faked:

1. `TestUserSearchQueryConstruction` -- `build_user_search_statement` is
   a plain, DB-connection-free function (see its own docstring in
   `users.py` for why it was factored out this way). Compiling its
   returned `Select` against the REAL `postgresql` dialect (never
   executing it) and inspecting the resulting SQL text + bound
   parameters is a legitimate, standard way to verify the query's SHAPE
   is correct -- that `websearch_to_tsquery('simple', q)` is really
   being built, that `ts_rank` really drives the `ORDER BY`, that `@@`
   really gates the `WHERE`, and that `role`/`academic_level` filters
   are added if and only if the caller actually supplied them -- without
   needing a live database to do it. Matches this project's own
   established "verify hand-written SQL via `--sql`/compile review, no
   live Postgres available" posture (see every migration in
   `alembic/versions/`).
2. `TestUserSearchRouterValidation` -- real HTTP requests through a real
   `TestClient` against the real FastAPI app: authentication is
   required (401 with no token), and every query-parameter validation
   rule (`q` missing/empty/too long, `skip` negative, `limit` out of
   `[1, 100]`) is enforced by FastAPI itself (422) BEFORE the handler
   body -- and therefore before `build_user_search_statement` -- ever
   runs, so these are safe to exercise against the real route with no
   database execution involved at all.
"""

import uuid

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.api.deps import get_current_user
from app.api.v1.endpoints.users import build_user_search_statement
from app.main import app
from app.models.enums import AcademicLevel, UserRole
from app.models.user import User

# DISCLOSED, DELIBERATE: no `pytestmark = pytest.mark.asyncio` here, unlike
# most other test files in this suite. Every test in this file is
# genuinely synchronous -- `build_user_search_statement` is a plain,
# non-async function, `.compile(...)` never touches a database
# connection, and `TestClient` requests are synchronous by design (that's
# what makes `TestClient` usable in a plain `def test_...` at all). Adding
# the async mark here would be cargo-culted, not correct -- it produced
# real `PytestWarning`s on a first draft of this file (caught and
# removed, not shipped).


def _compile(stmt):
    """Compiles a `Select` against the real `postgresql` dialect WITHOUT
    executing it against any database -- no live Postgres connection is
    ever opened by this helper or anything that calls it. Returns the
    compiled object so both `.string` (the SQL text) and `.params` (the
    bound parameter values) can be asserted on."""
    return stmt.compile(dialect=postgresql.dialect())


class TestUserSearchQueryConstruction:
    def test_uses_websearch_to_tsquery_with_the_simple_config(self):
        compiled = _compile(build_user_search_statement("cairo mentor"))
        assert "websearch_to_tsquery" in compiled.string
        # The FIRST bound value passed to websearch_to_tsquery must be
        # the literal text search config 'simple' -- matching
        # `search_vector`'s own trigger-side config exactly (migration
        # `0030_user_search_vector`) -- and the SECOND must be the
        # caller's actual query string, unmodified.
        assert "simple" in compiled.params.values()
        assert "cairo mentor" in compiled.params.values()

    def test_uses_the_at_at_match_operator_against_search_vector(self):
        compiled = _compile(build_user_search_statement("cairo"))
        assert "users.search_vector @@ websearch_to_tsquery" in compiled.string

    def test_orders_by_ts_rank_descending(self):
        compiled = _compile(build_user_search_statement("cairo"))
        assert "ORDER BY ts_rank(users.search_vector, websearch_to_tsquery" in compiled.string
        assert "DESC" in compiled.string

    def test_search_vector_itself_is_not_selected(self):
        # `User.search_vector` is `deferred=True` (app/models/user.py) --
        # an internal search-index artifact, not response content (see
        # `UserOut`'s own comment on deliberately excluding it). Confirms
        # that posture actually holds for this specific query, not just
        # in theory: the SELECT column list (the text before the first
        # "FROM") must not mention it, even though the WHERE/ORDER BY
        # clauses legitimately reference it as a filter/ranking input.
        compiled = _compile(build_user_search_statement("cairo"))
        select_columns = compiled.string.split("FROM users")[0]
        assert "search_vector" not in select_columns

    def test_role_filter_omitted_when_not_provided(self):
        # `users.role` legitimately appears in the plain SELECT column
        # list (it's an ordinary, non-deferred `User` column, unrelated
        # to whether a role FILTER was requested) -- what must be absent
        # is a `WHERE ... users.role = ...` comparison specifically.
        compiled = _compile(build_user_search_statement("cairo"))
        assert "users.role = " not in compiled.string

    def test_role_filter_applied_when_provided(self):
        compiled = _compile(build_user_search_statement("cairo", role=UserRole.MENTOR))
        assert "users.role = " in compiled.string
        assert UserRole.MENTOR in compiled.params.values()

    def test_academic_level_filter_omitted_when_not_provided(self):
        compiled = _compile(build_user_search_statement("cairo"))
        assert "users.academic_level = " not in compiled.string

    def test_academic_level_filter_applied_when_provided(self):
        compiled = _compile(
            build_user_search_statement("cairo", academic_level=AcademicLevel.UNIVERSITY_YEAR_2)
        )
        assert "users.academic_level = " in compiled.string
        assert AcademicLevel.UNIVERSITY_YEAR_2 in compiled.params.values()

    def test_both_filters_applied_together_are_and_ed_not_or_ed(self):
        compiled = _compile(
            build_user_search_statement(
                "cairo", role=UserRole.MENTOR, academic_level=AcademicLevel.MASTERS
            )
        )
        assert "AND users.academic_level = " in compiled.string

    def test_skip_and_limit_are_bound_as_offset_and_limit(self):
        compiled = _compile(build_user_search_statement("cairo", skip=40, limit=15))
        assert "LIMIT" in compiled.string
        assert "OFFSET" in compiled.string
        assert 40 in compiled.params.values()
        assert 15 in compiled.params.values()

    def test_default_skip_and_limit(self):
        compiled = _compile(build_user_search_statement("cairo"))
        assert 0 in compiled.params.values()
        assert 20 in compiled.params.values()


# ---------------------------------------------------------------------
# Router-layer validation -- real HTTP requests, real FastAPI validation,
# no database execution (every case here is rejected before the handler
# body, hence before `build_user_search_statement`, ever runs).
# ---------------------------------------------------------------------


@pytest.fixture
def authed_client():
    """Overrides `get_current_user` with a fake, already-authenticated
    `User` -- same established pattern as every other router-layer test
    in this codebase (e.g. `tests/test_live_sessions.py::TestLiveSessionRouterHTTP`)
    that only needs SOME authenticated identity, not real Supabase JWT
    verification, to exercise a route's own validation/authorization
    logic. A plain, synchronous fixture (not `pytest_asyncio.fixture`) --
    nothing inside it is awaited."""
    fake_user = User(id=uuid.uuid4(), email="searcher@example.com", is_active=True)
    app.dependency_overrides[get_current_user] = lambda: fake_user
    yield TestClient(app)
    app.dependency_overrides.pop(get_current_user, None)


class TestUserSearchRouterValidation:
    def test_requires_authentication(self):
        # No dependency override here -- a real, unauthenticated request.
        client = TestClient(app)
        response = client.get("/api/v1/users/search", params={"q": "cairo"})
        assert response.status_code == 401

    def test_missing_q_is_rejected(self, authed_client: TestClient):
        response = authed_client.get("/api/v1/users/search")
        assert response.status_code == 422

    def test_empty_q_is_rejected(self, authed_client: TestClient):
        response = authed_client.get("/api/v1/users/search", params={"q": ""})
        assert response.status_code == 422

    def test_overly_long_q_is_rejected(self, authed_client: TestClient):
        response = authed_client.get("/api/v1/users/search", params={"q": "a" * 201})
        assert response.status_code == 422

    def test_negative_skip_is_rejected(self, authed_client: TestClient):
        response = authed_client.get(
            "/api/v1/users/search", params={"q": "cairo", "skip": -1}
        )
        assert response.status_code == 422

    def test_limit_below_minimum_is_rejected(self, authed_client: TestClient):
        response = authed_client.get(
            "/api/v1/users/search", params={"q": "cairo", "limit": 0}
        )
        assert response.status_code == 422

    def test_limit_above_maximum_is_rejected(self, authed_client: TestClient):
        response = authed_client.get(
            "/api/v1/users/search", params={"q": "cairo", "limit": 101}
        )
        assert response.status_code == 422

    def test_invalid_role_is_rejected(self, authed_client: TestClient):
        response = authed_client.get(
            "/api/v1/users/search", params={"q": "cairo", "role": "not_a_real_role"}
        )
        assert response.status_code == 422

    def test_invalid_academic_level_is_rejected(self, authed_client: TestClient):
        response = authed_client.get(
            "/api/v1/users/search",
            params={"q": "cairo", "academic_level": "not_a_real_level"},
        )
        assert response.status_code == 422
