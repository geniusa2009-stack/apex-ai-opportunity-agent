"""APEX Privacy + Social Identity Layer -- the ONE centralized place
that answers "what can THIS viewer see about THIS target user?", reused
by both `GET /users/search` and `GET /users/{user_id}/profile`. Never
duplicate this filtering logic in a router or in the frontend -- per
this phase's own Rule 3 ("server is the security boundary") and Rule 7
("no security theater"), every privacy rule enforced here is the ONLY
enforcement; there is no client-side mirror of it anywhere.

AUDITED FIRST: no relationship-resolution or field-filtering helper
existed anywhere in this codebase before this step. `social_service.
_get_friend_ids` (accepted connections, either direction) already
existed and is the correct, existing primitive to build "connection"
resolution on top of -- reused here via the new public
`social_service.get_relationship` wrapper rather than re-implemented.
"""

from __future__ import annotations

import uuid

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.models.user_privacy import UserPrivacySettings
from app.schemas.privacy import PrivacyVisibility, ProfileViewOut, PublicSearchProfileOut, Relationship

_VISIBILITY_CATEGORIES = (
    "bio_visibility",
    "education_visibility",
    "skills_visibility",
    "location_visibility",
    "languages_visibility",
)


async def get_or_create_settings(db: AsyncSession, user_id: uuid.UUID) -> UserPrivacySettings:
    """Read-mostly lookup, same lazy-create-with-SAVEPOINT-race-guard
    posture as `notification_preferences.get_or_create_preferences` --
    returns the existing row if present, otherwise creates and flushes
    (not commits -- the caller decides transaction boundaries) a new,
    all-defaults ("public") row."""
    existing = await db.get(UserPrivacySettings, user_id)
    if existing is not None:
        return existing

    try:
        async with db.begin_nested():
            settings = UserPrivacySettings(user_id=user_id)
            db.add(settings)
            await db.flush()
    except IntegrityError:
        existing = await db.get(UserPrivacySettings, user_id)
        if existing is not None:
            return existing
        raise
    return settings


def _visible(visibility: PrivacyVisibility, relationship: Relationship) -> bool:
    """A field with the given category `visibility` is shown to a
    viewer with the given `relationship` to the target. `self` always
    sees everything about themselves; otherwise "public" is shown to
    anyone, "connections" only to an accepted connection, "private" to
    no one but the target."""
    if relationship == "self":
        return True
    if visibility == "public":
        return True
    if visibility == "connections":
        return relationship == "connection"
    return False  # "private"


def _default_settings_view() -> dict[str, PrivacyVisibility]:
    return {category: "public" for category in _VISIBILITY_CATEGORIES}


def _resolve_visibility(settings: UserPrivacySettings | None) -> dict[str, PrivacyVisibility]:
    """`settings=None` (no row yet) -> every category defaults to
    "public" (see module docstring). A settings ROW that exists but has
    an unset/None value on some category (never happens through the
    real DB, whose columns are all `nullable=False` with a
    `server_default` -- but defensively handled here anyway, e.g. for a
    plain Python-constructed object in a test that never went through a
    flush/refresh) also falls back to "public" per category, rather
    than silently treating a missing value as the strictest ("private")
    option -- a category this function can't confidently classify must
    never accidentally lock a user out of their own already-public
    profile field."""
    if settings is None:
        return _default_settings_view()
    return {
        category: getattr(settings, category, None) or "public" for category in _VISIBILITY_CATEGORIES
    }


def _scalar(value):
    """Enum columns (e.g. `User.role`, `User.academic_level`) come back
    as real enum members from a live SQLAlchemy session, but this
    module's own unit tests (and any other plain-Python construction)
    may hand it a bare string -- accept either rather than assuming."""
    return value.value if hasattr(value, "value") else value


def filter_search_row(
    target: User,
    settings: UserPrivacySettings | None,
    relationship: Relationship,
    connection_state: str = "none",
) -> PublicSearchProfileOut:
    """Builds one `GET /users/search` response row, nulling out any
    field the target's privacy settings + relationship to the viewer
    don't permit. `settings=None` (no row yet -- the target has never
    opened Privacy & Visibility) is treated as every category at its
    real default ("public"), matching `UserPrivacySettings`'s own
    documented lazy-create semantics.

    `connection_state` (NETWORK 2.0) is a separate, UI-only
    classification -- see `app/schemas/privacy.ConnectionState`'s own
    docstring -- passed through as-is from the caller's already-resolved
    bulk lookup; this function never derives it itself, same posture as
    `relationship`."""
    vis = _resolve_visibility(settings)

    bio = target.bio if _visible(vis["bio_visibility"], relationship) else None
    education_ok = _visible(vis["education_visibility"], relationship)
    skills = target.skills if _visible(vis["skills_visibility"], relationship) else None

    return PublicSearchProfileOut(
        id=target.id,
        full_name=target.full_name,
        role=_scalar(target.role),
        connection_state=connection_state,
        bio=bio,
        major=target.major if education_ok else None,
        degree_level=target.degree_level if education_ok else None,
        academic_level=(_scalar(target.academic_level) if target.academic_level else None) if education_ok else None,
        skills=skills,
    )


def build_profile_view(
    target: User,
    settings: UserPrivacySettings | None,
    relationship: Relationship,
    connection_state: str = "none",
) -> ProfileViewOut:
    """Builds a `GET /users/{user_id}/profile` response -- same field-
    level filtering as `filter_search_row`, plus the additional
    location/language/graduation-year fields that endpoint (unlike the
    lighter-weight search listing) also surfaces. `connection_state`:
    see `filter_search_row`'s own docstring."""
    vis = _resolve_visibility(settings)

    education_ok = _visible(vis["education_visibility"], relationship)
    skills_ok = _visible(vis["skills_visibility"], relationship)
    location_ok = _visible(vis["location_visibility"], relationship)
    languages_ok = _visible(vis["languages_visibility"], relationship)

    return ProfileViewOut(
        id=target.id,
        full_name=target.full_name,
        role=_scalar(target.role),
        relationship=relationship,
        connection_state=connection_state,
        bio=target.bio if _visible(vis["bio_visibility"], relationship) else None,
        major=target.major if education_ok else None,
        degree_level=target.degree_level if education_ok else None,
        academic_level=(_scalar(target.academic_level) if target.academic_level else None) if education_ok else None,
        graduation_year=target.graduation_year if education_ok else None,
        skills=target.skills if skills_ok else None,
        languages=target.languages if languages_ok else None,
        target_country=target.target_country if location_ok else None,
        nationality=target.nationality if location_ok else None,
        residency_country=target.residency_country if location_ok else None,
    )
