"""Schemas for the APEX Privacy + Social Identity Layer:
`UserPrivacySettings` read/write, and the two safe, data-minimized,
per-viewer response shapes (`PublicSearchProfileOut`,
`ProfileViewOut`) that replace returning the full `UserOut` to other
users. Never import `UserOut` for a cross-user response -- that was
the exact vulnerability this step fixes (`GET /users/search` leaking
gpa/phone/gender/email to any signed-in user)."""

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

# Same "Literal enforces on write, column stays plain String" pattern as
# `AchievementPost.privacy_level` / `UserConnection.status` elsewhere in
# this codebase.
PrivacyVisibility = Literal["public", "connections", "private"]

Relationship = Literal["self", "connection", "stranger"]

# NETWORK 2.0: a finer-grained, UI-only classification than
# `Relationship` above -- which exists purely to drive PRIVACY field
# filtering (self sees everything, a connection sees "connections"-
# visibility fields, anyone else sees only "public" ones) and has no
# reason to distinguish a pending outgoing request from a pending
# incoming one, since neither unlocks any extra field. `ConnectionState`
# is a SEPARATE, additive classification resolved from the exact same
# `UserConnection` rows (see `social_service.get_connection_state`) so
# the frontend can render the right action button (Connect / Request
# Sent / Accept+Decline / Message) without a second social-graph model
# and without ever guessing or trusting a client-supplied state.
ConnectionState = Literal["self", "connected", "request_sent", "request_received", "none"]


class UserPrivacySettingsOut(BaseModel):
    """GET /users/me/privacy-settings response."""

    model_config = ConfigDict(from_attributes=True)

    bio_visibility: PrivacyVisibility
    education_visibility: PrivacyVisibility
    skills_visibility: PrivacyVisibility
    location_visibility: PrivacyVisibility
    languages_visibility: PrivacyVisibility
    updated_at: datetime


class UserPrivacySettingsUpdate(BaseModel):
    """PATCH /users/me/privacy-settings request body. Every field
    optional/independently settable -- same established `exclude_unset`
    "None means leave unchanged" semantics as `UserProfileUpdate`."""

    bio_visibility: PrivacyVisibility | None = None
    education_visibility: PrivacyVisibility | None = None
    skills_visibility: PrivacyVisibility | None = None
    location_visibility: PrivacyVisibility | None = None
    languages_visibility: PrivacyVisibility | None = None


class PublicSearchProfileOut(BaseModel):
    """GET /users/search response row -- replaces the old, unrestricted
    `UserOut`. `email`, `phone`, `gpa`, `gender`, `is_active`,
    `created_at` are deliberately never included here: they are
    SENSITIVE-category fields (per the audit's own data classification)
    that no frontend consumer of this endpoint (`mentors.html`,
    `mentor-profile.html`, `network.html`) reads, and returning them to
    every signed-in user was the confirmed vulnerability.

    `bio`/`major`/`degree_level`/`academic_level`/`skills` are each
    nulled out server-side (in `app/services/privacy_service.py`) when
    the target's own privacy settings and relationship to the viewer
    say they shouldn't be shown -- never filtered client-side."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str | None
    role: str
    connection_state: ConnectionState
    bio: str | None
    major: str | None
    degree_level: str | None
    academic_level: str | None
    skills: list[str] | None


class ProfileViewOut(BaseModel):
    """GET /users/{user_id}/profile response -- the canonical,
    centralized "what can THIS viewer see about THIS user" view.
    `relationship` is server-resolved (never client-supplied), so the
    frontend can render a "You" / "Connection" / distinct public-profile
    chrome without re-deriving it. Personal-intelligence fields
    (interests, application goals, funding/remote preferences, etc.)
    are deliberately absent here -- self-only, never shown to another
    viewer regardless of relationship."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    full_name: str | None
    role: str
    relationship: Relationship
    connection_state: ConnectionState

    bio: str | None
    major: str | None
    degree_level: str | None
    academic_level: str | None
    graduation_year: int | None
    skills: list[str] | None
    languages: list[str] | None
    target_country: str | None
    nationality: str | None
    residency_country: str | None

