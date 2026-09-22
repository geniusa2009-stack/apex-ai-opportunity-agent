from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field

from app.models.enums import AcademicLevel, ApplicationGoal, Gender, OpportunityType, UserRole

# `UserCreate` and `Token` were removed as part of the Supabase
# integration step -- both existed solely for the local `POST
# /auth/register` / `POST /auth/login` endpoints, which were deleted
# once Supabase became the sole authentication authority (per explicit
# approval; see README). Nothing else in the codebase referenced either
# schema (confirmed via a full-repo grep before removing).


class UserOut(BaseModel):
    """Public shape of a User. Never includes `hashed_password`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    # Nullable as of the Supabase integration step -- see the comment on
    # `User.email` in app/models/user.py for why (Supabase's `email`
    # claim is genuinely optional, e.g. for phone-only/anonymous sign-ins).
    email: EmailStr | None
    full_name: str | None
    is_active: bool
    created_at: datetime

    # Student profile fields (Step 3.20) -- all optional, self-reported.
    phone: str | None
    degree_level: str | None
    major: str | None
    gpa: float | None
    graduation_year: int | None
    skills: list[str] | None

    # Added for Step 3.27 -- see app/models/user.py's comment on this column.
    target_country: str | None

    # Added for the Student Dashboard & Admin Control Panel step -- see
    # app/models/user.py's comment on this column.
    age_group: str | None

    # Added for the Backend Folder Structure & Core Database Models step
    # -- see app/models/user.py's comments on these two columns.
    # Additive alongside `age_group` above, not a replacement.
    age: int | None
    academic_level: AcademicLevel | None

    # Added for the User Directory Search step -- see app/models/user.py's
    # comment on this column. `search_vector` itself is deliberately NOT
    # exposed here: it's an internal search-index artifact (raw tsvector
    # lexeme/weight/position data), not user-facing profile content, and
    # `User.search_vector` is `deferred=True` besides -- reading it via a
    # `UserOut` response would force an extra query on every profile fetch
    # for a field no client of this API has any real use for.
    bio: str | None

    # Added for the User Directory Search step (endpoint pass) -- see
    # `UserRole`'s own docstring (app/models/enums.py). Always present
    # (the column is NOT NULL with a `STUDENT` default), unlike every
    # nullable profile field above it.
    role: UserRole

    # Added for the Real Age/Academic-Year/Gender Opportunity Filtering
    # step -- see app/models/user.py's comment on this column and
    # `Gender`'s own docstring (app/models/enums.py) for the "confirmed
    # genuinely net-new" disclosure.
    gender: Gender | None


class UserProfileUpdate(BaseModel):
    """PATCH body for updating the profile fields added in Step 3.20.
    Every field optional and independently settable -- a caller updating
    just `major` shouldn't be forced to resend everything else, and
    `None` here means "leave unchanged," not "clear this field" (there's
    no dedicated "clear" semantics yet -- see the README's Not Included
    list for this step)."""

    full_name: str | None = None
    phone: str | None = Field(default=None, max_length=50)
    degree_level: str | None = Field(default=None, max_length=100)
    major: str | None = Field(default=None, max_length=255)
    # Deliberately NOT capped at le=10.0 or similar -- GPA scales vary
    # internationally (4.0, 5.0, 10.0, 100-point are all common; see the
    # same scale-agnostic reasoning already used for
    # ExtractedDocumentSchema.gpa_scale in vision.py, Step 3.4). 0-100
    # covers every common scale without silently rejecting a valid
    # non-4.0-scale value; there's no `gpa_scale` field yet to record
    # which scale a given number is on (see Not Included below).
    gpa: float | None = Field(default=None, ge=0.0, le=100.0)
    graduation_year: int | None = Field(default=None, ge=1950, le=2100)
    skills: list[str] | None = None
    target_country: str | None = Field(default=None, max_length=120)
    # Added for the Student Dashboard & Admin Control Panel step -- e.g.
    # 'prep_secondary', 'university', 'graduates' (same free-text
    # vocabulary as `Opportunity.target_age_group`; see app/models/user.py).
    age_group: str | None = Field(default=None, max_length=50)

    # Added for the Backend Folder Structure & Core Database Models step
    # -- additive alongside `age_group` above, not a replacement (see
    # app/models/enums.py's `AcademicLevel` docstring).
    age: int | None = Field(default=None, ge=0, le=120)
    academic_level: AcademicLevel | None = None

    # Added for the User Directory Search step -- see app/models/user.py's
    # comment on `User.bio`. 2000 chars, matching this codebase's own
    # established "a real but bounded free-text field" cap (e.g.
    # `LiveSessionCompleteRequest.feedback`), not unbounded -- a directory
    # bio is a short self-introduction, not a full essay.
    bio: str | None = Field(default=None, max_length=2000)

    # Added for the User Directory Search step (endpoint pass) -- see
    # `UserRole`'s own docstring. Self-reported like every other field on
    # this schema; `None` here means "leave unchanged" (this schema's own
    # established `exclude_unset` semantics), NOT "reset to the STUDENT
    # default" -- a caller who wants to actually change their role must
    # send an explicit `role` value.
    role: UserRole | None = None

    # Added for the Real Age/Academic-Year/Gender Opportunity Filtering
    # step -- self-reported like every other field on this schema;
    # `None` means "leave unchanged," same established semantics.
    gender: Gender | None = None

    # Added for the Personalized Opportunity Intelligence + Opportunity
    # Passport step (Part A: User Opportunity Profile). Every field
    # optional/independently settable, same established `PATCH`
    # semantics as every field above: `None` means "leave unchanged."
    nationality: str | None = Field(default=None, max_length=120)
    residency_country: str | None = Field(default=None, max_length=120)
    languages: list[str] | None = None
    interests: list[str] | None = None
    application_goals: list[ApplicationGoal] | None = None
    preferred_opportunity_types: list[OpportunityType] | None = None
    remote_preference: bool | None = None
    funding_preference: str | None = Field(default=None, max_length=30)
    deadline_lead_time_days: int | None = Field(default=None, ge=0, le=365)
