import datetime as dt
import logging
import uuid

from fastapi import APIRouter, HTTPException, Query, status
from sqlalchemy import Select, func, select

from app.api.deps import CurrentUser, DbSession
from app.models.enums import AcademicLevel, UserRole
from app.models.user import User
from app.schemas.privacy import (
    ProfileViewOut,
    PublicSearchProfileOut,
    UserPrivacySettingsOut,
    UserPrivacySettingsUpdate,
)
from app.schemas.user import (
    OnboardingCompleteRequest,
    OnboardingCompleteResponse,
    OnboardingTaxonomyNotes,
    UserOut,
    UserProfileUpdate,
)
from app.services import age as age_service
from app.services import blacklist_service, onboarding_taxonomy, privacy_service, social_service
from app.services.onboarding_intent_extraction import onboarding_intent_extractor

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["users"])

# New in Step 3.20 (Advanced Feature 6): these two endpoints didn't exist
# before this step. They're the direct consequence of the chosen
# resolution to a gap found while building the Auto-Fill Agent -- the
# User table had nowhere to store phone/degree/major/gpa/graduation_year/
# skills, and the model was extended to add them. Extending the model
# with no way to actually read or write those fields would leave the
# schema change inert, so these were added alongside it -- not part of
# the literal Feature 6 ask, but necessary for it to do anything real.


@router.get("/me", response_model=UserOut)
async def get_my_profile(current_user: CurrentUser) -> User:
    return current_user


@router.patch("/me", response_model=UserOut)
async def update_my_profile(
    payload: UserProfileUpdate, current_user: CurrentUser, db: DbSession
) -> User:
    # `exclude_unset` -- only fields the caller actually included in the
    # request body are applied. This matters because `None` is a
    # meaningful "leave unchanged" default on every field in
    # `UserProfileUpdate`; without `exclude_unset`, a partial PATCH body
    # like `{"major": "Physics"}` would silently null out every other
    # field the caller didn't mention.
    updates = payload.model_dump(exclude_unset=True)

    # General Content Blacklist / Pattern Auto-Trigger addition -- `bio`
    # is the one free-text field on this profile that becomes visible to
    # OTHER students (via `GET /users/search`'s full-text directory
    # search, which indexes and returns it) -- every other field on
    # `UserProfileUpdate` (major, skills, etc.) is structured/short
    # metadata, not the kind of free-text field this task's own Step 1.2
    # is about. Only checked when `bio` is actually part of THIS PATCH
    # (`exclude_unset` already narrows `updates` to fields the caller
    # sent), matching this endpoint's own existing partial-update
    # posture.
    if "bio" in updates:
        blacklist_match = await blacklist_service.check_content(db, updates["bio"])
        if blacklist_match is not None:
            blacklist_service.log_trigger(
                db, submitting_user_id=current_user.id, match=blacklist_match,
                content_type="profile_bio", content_snippet=updates["bio"],
            )
            await db.commit()
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="This bio can't be saved -- it contains content that isn't allowed here.",
            )

    for field, value in updates.items():
        setattr(current_user, field, value)

    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)
    return current_user


# ---------------------------------------------------------------------
# APEX Onboarding Production Integration
# ---------------------------------------------------------------------
#
# A dedicated completion endpoint, not a bare reuse of `PATCH /users/me`
# above, because this one submission needs validation that endpoint
# deliberately does not perform generically for every partial profile
# edit: a hard server-side minimum-age check (Section 5 of the
# governing directive) and deterministic taxonomy mapping from the
# onboarding UI's own vocabulary (Section 9). It still writes through
# the SAME `User` model fields `UserProfileUpdate`/`update_my_profile`
# already use -- no second profile table, no second write path.
#
# IDEMPOTENT BY CONSTRUCTION: every write here is a plain overwrite of
# fields on the caller's own `current_user` row (same as `PATCH
# /users/me`) -- a refresh, a lost-connection retry, or a duplicate
# submission just re-applies the same values; nothing here INSERTs a
# new row or appends to a log per call. `onboarding_completed_at` is
# set only the first time (never reset on a later resubmission), which
# is what makes it a trustworthy "did this user ever finish onboarding"
# signal rather than a re-settable client flag.
@router.post("/me/onboarding/complete", response_model=OnboardingCompleteResponse)
async def complete_onboarding(
    payload: OnboardingCompleteRequest, current_user: CurrentUser, db: DbSession
) -> OnboardingCompleteResponse:
    # SECURITY (Section 20): the client's own age/eligibility computation
    # is never trusted -- this is the one real, authoritative check, run
    # here regardless of what the onboarding UI's client-side `blocked()`
    # already did or didn't catch.
    try:
        age_service.validate_minimum_age(payload.date_of_birth)
    except age_service.UnderMinimumAgeError as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    academic_level, unmapped_academic_level = onboarding_taxonomy.map_stage_to_academic_level(
        payload.stage, payload.grade
    )
    preferred_opportunity_types, unmapped_opportunity_types = onboarding_taxonomy.map_looking_for(
        payload.looking_for
    )
    application_goals, unmapped_goal = onboarding_taxonomy.map_goal(payload.goal)
    funding_preference, unmapped_funding = onboarding_taxonomy.map_funding(payload.funding)
    remote_preference = onboarding_taxonomy.map_where(payload.where)

    primary_nationality, *rest_nationalities = payload.nationalities

    # EXPLICIT PROFILE PERSISTENCE -- this entire block is the one thing
    # that must succeed for onboarding to succeed (Section 19/22: AI
    # enrichment below is optional and must never block this).
    current_user.date_of_birth = payload.date_of_birth
    current_user.age = age_service.compute_age(payload.date_of_birth)
    current_user.full_name = (
        f"{payload.first_name.strip()} {payload.last_name.strip()}".strip()
        if payload.last_name
        else payload.first_name.strip()
    )
    current_user.residency_country = payload.country
    current_user.nationality = primary_nationality
    current_user.other_nationalities = rest_nationalities or None
    current_user.academic_level = academic_level
    current_user.english_level = payload.english_level
    # Explicit interests from Step 5 always take priority; see the AI
    # enrichment step below for how it may ADD to this list without
    # ever removing or reordering what the user picked.
    current_user.interests = list(payload.interests)
    current_user.preferred_opportunity_types = preferred_opportunity_types or None
    current_user.application_goals = [g.value for g in application_goals] or None
    current_user.funding_preference = funding_preference
    current_user.remote_preference = remote_preference

    if current_user.onboarding_completed_at is None:
        current_user.onboarding_completed_at = dt.datetime.now(dt.timezone.utc)

    db.add(current_user)
    await db.commit()
    await db.refresh(current_user)

    # AI ENRICHMENT (Section 12/13) -- best-effort, server-side only,
    # and strictly ADDITIVE: it can only append a NEW tag not already
    # in the user's own explicit `interests` (case-insensitive
    # comparison), never overwrite or reorder the explicit list. Any
    # failure here (provider error, timeout, malformed response) is
    # swallowed -- the explicit profile above is already committed and
    # onboarding has already succeeded regardless of what happens next.
    ai_enrichment_applied = False
    if payload.intent and payload.intent.strip():
        try:
            suggested_tags = await onboarding_intent_extractor.extract_applicable_tags(
                payload.intent.strip()
            )
        except Exception:
            logger.warning(
                "Onboarding intent extraction failed for user %s -- "
                "continuing with explicit profile only",
                current_user.id,
                exc_info=True,
            )
        else:
            existing_lower = {i.lower() for i in (current_user.interests or [])}
            new_tags = [t for t in suggested_tags if t.lower() not in existing_lower]
            if new_tags:
                current_user.interests = list(current_user.interests or []) + new_tags
                db.add(current_user)
                await db.commit()
                await db.refresh(current_user)
                ai_enrichment_applied = True

    return OnboardingCompleteResponse(
        profile=current_user,
        taxonomy_notes=OnboardingTaxonomyNotes(
            unmapped_academic_level=unmapped_academic_level,
            unmapped_opportunity_types=unmapped_opportunity_types,
            unmapped_goal=unmapped_goal,
            unmapped_funding=unmapped_funding,
        ),
        ai_enrichment_applied=ai_enrichment_applied,
        onboarding_completed_at=current_user.onboarding_completed_at,
    )


# --- User Directory Search (endpoint pass, on top of the schema/trigger
# groundwork laid in migrations 0030/0031) ---
#
# DISCLOSED ROUTE-ORDERING NOTE: FastAPI matches routes in declaration
# order, and `/search` -- a literal path segment, not a path parameter --
# cannot collide with `/me` above it either way; unlike a hypothetical
# future `GET /users/{user_id}`, no ordering precaution is needed here.
#
# DISCLOSED PARAMETER NAMING DEVIATION: every other paginated list
# endpoint in this codebase (`opportunities.py`, `history.py`,
# `student_videos.py`, `bookmarks.py`, `admin_moderation.py`) uses
# `limit`/`offset`. This endpoint uses `skip`/`limit` instead, exactly as
# this step's own ask specifies by name -- honored literally rather than
# silently renamed to match the ambient convention, since the ask was
# explicit about this particular parameter name. `skip` and `offset` mean
# the exact same thing (`.offset(skip)` below).
#
# DISCLOSED REQUIRED-`q` CHOICE: `q` is REQUIRED and non-empty
# (`min_length=1`), not optional-with-a-role-only-fallback -- matches
# this exact codebase's own existing precedent for a search endpoint,
# `student_videos.py`'s `q: str = Query(..., min_length=1, max_length=500,
# ...)` (its own natural-language AI search route), rather than inventing
# a second, different "empty query" posture for this one. A request with
# no `q` at all, or an empty string, is rejected with FastAPI's own
# built-in 422 validation error -- "graceful" here means a clear,
# standard validation error, not silently reinterpreting the request as
# a different kind of query the caller didn't ask for.
def build_user_search_statement(
    q: str,
    *,
    role: UserRole | None = None,
    academic_level: AcademicLevel | None = None,
    skip: int = 0,
    limit: int = 20,
) -> Select[tuple[User]]:
    """The actual FTS query-building logic, deliberately factored OUT of
    `search_users` below into its own plain, synchronous, DB-connection-
    free function. Two real reasons, not just tidiness:

    1. It's independently unit-testable. This project has NO live
       Postgres available in its own sandbox (a disclosed, standing
       limitation -- see migration `0030_user_search_vector`'s own
       README entry), and `websearch_to_tsquery`/`ts_rank`/`@@` are all
       genuinely Postgres-only -- SQLite (this project's only available
       test-time database) raises `OperationalError: no such function`
       the instant any of them is actually EXECUTED. Compiling this
       statement against the `postgresql` dialect (never executing it)
       and asserting on the resulting SQL text is the only verification
       this sandbox can actually perform on the query-construction logic
       itself -- see `tests/test_user_search.py`'s own module docstring.
    2. It keeps `search_users` itself a thin "call this, then await
       `db.execute(...)`" wrapper, so there is exactly ONE place this
       query's shape is defined, not two (a duplicate, hand-rewritten
       copy living only in a test file, silently drifting from the real
       endpoint over time, would be worse than not testing it at all).

    `websearch_to_tsquery('simple', q)` -- NOT `plainto_tsquery` or a
    hand-built `to_tsquery` -- parses `q` using the same forgiving,
    web-search-engine-style syntax Google/DuckDuckGo users already know
    (quoted phrases, `-exclude`, bare `OR`), safely: unlike raw
    `to_tsquery`, it never raises a syntax error on malformed input (a
    lone trailing `"`, an `&` with nothing after it) -- it degrades
    gracefully instead, exactly the "handle messy input gracefully"
    posture this endpoint needs for free-form user input. `'simple'`
    matches `search_vector`'s own text search configuration exactly (see
    migration `0030_user_search_vector`'s own disclosed reasoning on
    mixed Arabic/English content) -- using a different configuration here
    than the one the trigger used to BUILD `search_vector` would silently
    produce wrong/inconsistent matches.

    `User.search_vector.op("@@")(query)` -- the actual match filter.
    SQLAlchemy has no built-in `@@` operator method, so `.op("@@")` is
    the standard, documented way to emit a raw Postgres operator through
    the expression language while keeping every operand itself still a
    proper bound SQL expression (parameterized query, no string
    interpolation -- `q` is never concatenated into raw SQL anywhere in
    this function).

    `ts_rank(User.search_vector, query)` -- computed once, reused for the
    `ORDER BY ... DESC` (best matches first) and left un-selected
    otherwise (this endpoint returns `UserOut` rows, not rank scores; the
    ask's own response shape names `UserOut`, not a rank-annotated
    variant). Ordering by an expression that isn't part of the selected
    columns is valid, ordinary SQL -- no `GROUP BY`/`DISTINCT` is involved
    here that would require it to be.

    `role`/`academic_level` -- applied as plain exact-match `WHERE`
    clauses, each added only if the caller actually supplied that filter
    (an omitted filter must not silently exclude every row that happens
    to have a different value in that column).
    """
    query = func.websearch_to_tsquery("simple", q)
    rank = func.ts_rank(User.search_vector, query)

    stmt = select(User).where(User.search_vector.op("@@")(query))
    if role is not None:
        stmt = stmt.where(User.role == role)
    if academic_level is not None:
        stmt = stmt.where(User.academic_level == academic_level)
    return stmt.order_by(rank.desc()).offset(skip).limit(limit)


@router.get("/search", response_model=list[PublicSearchProfileOut])
async def search_users(
    db: DbSession,
    current_user: CurrentUser,  # directory search requires being signed in, like every other route in this router
    q: str = Query(..., min_length=1, max_length=200, description="Full-text search query (name, skills, bio)."),
    role: UserRole | None = Query(default=None, description="Filter by directory role."),
    academic_level: AcademicLevel | None = Query(default=None, description="Filter by academic level."),
    skip: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
) -> list[PublicSearchProfileOut]:
    """`GET /api/v1/users/search` -- Postgres full-text search over the
    user directory, ranked by relevance. See `build_user_search_statement`
    above for the actual query-construction logic and its full disclosed
    reasoning.

    PRIVACY + SOCIAL IDENTITY LAYER FIX: this used to return the full
    `UserOut` shape (including `email`, `phone`, `gpa`, `gender`,
    `is_active`, `created_at`) to ANY signed-in user -- the single
    highest-severity finding of this phase's audit. Now returns
    `PublicSearchProfileOut`, a minimized shape with those sensitive
    fields removed entirely (never conditionally shown -- there is no
    setting that exposes them), and with the remaining profile fields
    (`bio`/`major`/`degree_level`/`academic_level`/`skills`) filtered
    per-row through `privacy_service.filter_search_row`, which itself
    resolves each result's relationship to `current_user` via
    `social_service.get_relationship` and applies that result's own
    `UserPrivacySettings`. Server-enforced, not client-filtered (Rule 3):
    a field this endpoint doesn't return isn't hidden in JavaScript --
    it was never in the response.
    """
    stmt = build_user_search_statement(
        q, role=role, academic_level=academic_level, skip=skip, limit=limit
    )
    result = await db.execute(stmt)
    targets = list(result.scalars().all())
    if not targets:
        return []

    settings_result = await db.execute(
        select(privacy_service.UserPrivacySettings).where(
            privacy_service.UserPrivacySettings.user_id.in_([t.id for t in targets])
        )
    )
    settings_by_user = {row.user_id: row for row in settings_result.scalars().all()}

    # PERFORMANCE FIX (Final Hardening Gate): resolved once for every
    # result row together, not once PER row -- see `social_service.
    # get_relationships_bulk`'s docstring. This is exactly 1 additional
    # query for the whole page of results, not 1 per result.
    relationships = await social_service.get_relationships_bulk(
        db, current_user.id, [t.id for t in targets]
    )
    # NETWORK 2.0: same bulk-resolution shape as `relationships` above,
    # for the separate UI connection-state classification -- see
    # `social_service.get_connection_states_bulk`'s docstring. Also
    # exactly 1 additional query for the whole page, not 1 per result.
    connection_states = await social_service.get_connection_states_bulk(
        db, current_user.id, [t.id for t in targets]
    )

    return [
        privacy_service.filter_search_row(
            target,
            settings_by_user.get(target.id),
            relationships[target.id],
            connection_states[target.id],
        )
        for target in targets
    ]


@router.get("/me/privacy-settings", response_model=UserPrivacySettingsOut)
async def get_my_privacy_settings(current_user: CurrentUser, db: DbSession) -> UserPrivacySettingsOut:
    """New in the Privacy + Social Identity Layer phase. Lazily creates
    an all-"public" (== today's real, unchanged behavior) settings row
    on first read if the user has never opened Privacy & Visibility --
    see `UserPrivacySettings`'s own docstring for why "public" is the
    correct, non-regressing default rather than "connections"."""
    settings = await privacy_service.get_or_create_settings(db, current_user.id)
    await db.commit()
    await db.refresh(settings)
    return settings


@router.patch("/me/privacy-settings", response_model=UserPrivacySettingsOut)
async def update_my_privacy_settings(
    payload: UserPrivacySettingsUpdate, current_user: CurrentUser, db: DbSession
) -> UserPrivacySettingsOut:
    """Only ever reads/writes `current_user.id`'s own row -- there is no
    `user_id` in the request body, so there is no way for this endpoint
    to be pointed at another user's settings (Rule 3: the authorization
    boundary is that the row to modify is derived from the authenticated
    session, never from caller-supplied input)."""
    settings = await privacy_service.get_or_create_settings(db, current_user.id)
    updates = payload.model_dump(exclude_unset=True)
    for field, value in updates.items():
        setattr(settings, field, value)
    db.add(settings)
    await db.commit()
    await db.refresh(settings)
    return settings


@router.get("/{user_id}/profile", response_model=ProfileViewOut)
async def get_user_profile(user_id: uuid.UUID, current_user: CurrentUser, db: DbSession) -> ProfileViewOut:
    """New in the Privacy + Social Identity Layer phase: the canonical
    per-user profile-view endpoint named throughout this phase's own
    spec -- did not exist before at all (confirmed by an explicit
    disclosed design comment inside `mentor-profile.html` itself, and
    independently by this phase's own repository audit). Route-ordering
    note: declared AFTER the literal `/search` and `/me...` path
    segments above, so FastAPI's declaration-order matching never lets
    this `{user_id}` path parameter shadow them.

    Resolves the viewer's relationship to `user_id` server-side (never
    trusts a client-supplied relationship) and returns the SAME
    `privacy_service` filtering used by `/search`, just with the fuller
    field set (`ProfileViewOut`) a single-profile view needs."""
    target = await db.get(User, user_id)
    if target is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="User not found")

    relationship = await social_service.get_relationship(db, current_user.id, user_id)
    # NETWORK 2.0: resolved the same way, from the same `UserConnection`
    # rows -- see `social_service.get_connection_state`'s docstring for
    # why this is a separate classification from `relationship` above.
    connection_state = await social_service.get_connection_state(db, current_user.id, user_id)
    settings = await db.get(privacy_service.UserPrivacySettings, user_id)
    return privacy_service.build_profile_view(target, settings, relationship, connection_state)
