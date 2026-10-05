import uuid
from typing import TYPE_CHECKING

import datetime as dt

from sqlalchemy import Boolean, Date, DateTime, Float, Integer, String, Text
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, TSVECTOR, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db.base import Base
from app.models.enums import (
    AcademicLevel,
    AdminRole,
    ApplicationGoal,
    EnglishLevel,
    Gender,
    OpportunityType,
    UserRole,
)
from app.models.mixins import TimestampMixin

if TYPE_CHECKING:
    from app.models.bookmark import UserBookmark


class User(Base, TimestampMixin):
    __tablename__ = "users"

    # `default=uuid.uuid4` is effectively dead code as of the Supabase
    # integration step -- every `User` row is now created (in
    # app/api/deps.py's auto-provisioning path) with an explicit
    # `id=<the Supabase token's sub claim>`, never relying on this
    # default. Left in place anyway as a harmless fallback rather than
    # removed, in case anything ever constructs a `User` without an
    # explicit id (e.g. a test fixture).
    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    # Also newly nullable as of the Supabase integration step -- a
    # necessary follow-on to auto-provisioning, not something asked for
    # directly. Supabase's `email` claim is genuinely optional (phone-only
    # or anonymous-auth sign-ins may have no email at all), and
    # app/api/deps.py's auto-provisioning path passes whatever the token
    # actually contains straight through -- forcing NOT NULL here would
    # make those otherwise-valid users fail to auto-provision with a raw
    # IntegrityError instead of the friendlier experience of just having
    # an empty profile field.
    email: Mapped[str | None] = mapped_column(String(255), unique=True, index=True, nullable=True)
    # Nullable as of the Supabase integration step. Previously NOT NULL,
    # populated by the (now-removed) local `POST /auth/register`
    # endpoint. Supabase is now the sole authentication authority --
    # this backend never sees a user's actual password, so there is
    # nothing to hash and store here for a Supabase-authenticated user.
    # Column kept (not dropped) since `hash_password`/`verify_password`
    # are still available as generic utilities in app/core/security.py
    # for a possible future local/admin account path.
    hashed_password: Mapped[str | None] = mapped_column(String(255), nullable=True)
    full_name: Mapped[str | None] = mapped_column(String(255), nullable=True)

    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    is_superuser: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # --- Student profile fields (Step 3.20 / Advanced Feature 6) ---
    # Added specifically so `GET /autofill-profile` has real data to pull
    # from the database, per the resolution chosen when it turned out
    # this table previously had nowhere to store a rich profile (only
    # email/full_name existed). All nullable/self-reported -- a user may
    # not have filled any of these in yet, and nothing here is verified
    # against an external source (that's a different feature's job, if
    # ever built).
    phone: Mapped[str | None] = mapped_column(String(50), nullable=True)
    degree_level: Mapped[str | None] = mapped_column(
        String(100), nullable=True, doc="Free text, e.g. 'Undergraduate', 'Master's', 'PhD'."
    )
    major: Mapped[str | None] = mapped_column(String(255), nullable=True)
    gpa: Mapped[float | None] = mapped_column(Float, nullable=True)
    graduation_year: Mapped[int | None] = mapped_column(Integer, nullable=True)
    # Plain list of strings (Postgres ARRAY), not a comma-separated
    # string column -- storage stays structured; `skills_comma_separated`
    # is a presentation-layer format the autofill service produces on
    # the way out, not how this is actually persisted.
    skills: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True)

    # --- Added for Step 3.27 (Autonomous Scholar Agent) ---
    # That step's ask required "Target Country" as one of four profile
    # inputs the background worker reads (alongside GPA/Major/Degree
    # Level, all three of which already existed from Step 3.20) -- but
    # no country field existed on User. Same resolution already chosen
    # for the identical shape of gap in Step 3.20 (extend User rather
    # than leave the feature degraded), applied again without re-asking
    # since it's the same category of problem with an already-established
    # answer. Same nullable/self-reported/unverified trust model as every
    # other profile field, and immediately settable via the existing
    # `PATCH /users/me` (Step 3.20) -- no new endpoint needed.
    target_country: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # --- Added for the Student Dashboard & Admin Control Panel step ---
    # That step's ask requires the student hub's "Recommended Opportunities"
    # widget to return "opportunities matching the user's age group" -- but
    # no notion of a student's age, age bracket, or grade level existed
    # anywhere on `User` (confirmed via a full-model search: `graduation_year`
    # is the closest existing field, but a graduation year alone doesn't
    # cleanly map onto `Opportunity.target_age_group`'s own free-text
    # vocabulary -- see below). Same resolution already chosen twice before
    # for this identical shape of gap (`target_country` above, and the
    # original `phone`/`degree_level`/`major`/... block Step 3.20 added):
    # extend `User` with the missing profile field rather than leave the
    # feature degraded or silently ignore half of its own literal spec.
    #
    # Deliberately a free-text `String`, not a native enum -- matches
    # `Opportunity.target_age_group`'s OWN column exactly (see
    # app/models/opportunity.py), which this field exists specifically to
    # be compared against in the dashboard's own recommendation query
    # (`app/api/v1/endpoints/dashboards.py`). `aggregator_service.py`'s own
    # Gemini extraction prompt illustrates that column's real vocabulary --
    # `'prep_secondary'`, `'university'`, `'graduates'` -- reused here
    # rather than inventing a second, incompatible taxonomy. Nullable,
    # self-reported, unverified -- same trust model as every other profile
    # field on this model -- and immediately settable via the existing
    # `PATCH /users/me` (see `app/schemas/user.py`'s `UserProfileUpdate`),
    # no new endpoint needed, same precedent as `target_country`.
    age_group: Mapped[str | None] = mapped_column(String(50), nullable=True)

    # --- Added for the Backend Folder Structure & Core Database Models
    # step (a re-specification of this same profile-personalization
    # concept with two more precise fields than the coarse `age_group`
    # bucket above) ---
    #
    # `age` -- a genuinely new concept: nothing on `User` previously
    # captured an actual numeric age (only `graduation_year`, which
    # requires a calendar-math inference this ask's literal "age
    # (Integer)" field doesn't ask this codebase to perform). Nullable,
    # self-reported, unverified -- same trust model as every other
    # profile field on this model -- and immediately settable via the
    # existing `PATCH /users/me` (see `app/schemas/user.py`'s
    # `UserProfileUpdate`), no new endpoint needed, same precedent as
    # `target_country`/`age_group` above.
    age: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # `academic_level` -- a native Postgres ENUM (`AcademicLevel`, see
    # app/models/enums.py's own docstring for the full "why this is
    # additive alongside `age_group`, not a replacement" disclosure), a
    # materially stricter personalization signal than `age_group`'s
    # three coarse free-text buckets. Nullable/self-reported, same
    # `PATCH /users/me` wiring as every field above.
    academic_level: Mapped[AcademicLevel | None] = mapped_column(
        # `values_callable` -- disclosed fix, same defect and same fix as
        # `Opportunity.type`/`.target_academic_levels` (see
        # app/models/opportunity.py's own comment on this exact issue).
        # Without it, SQLAlchemy sends the Python enum MEMBER NAME
        # ("UNIVERSITY") to Postgres instead of `.value` ("university"),
        # but the native Postgres enum type holds the lowercase VALUES.
        # Confirmed as a real crash while testing admin-moderation
        # endpoints: `role` (below), which is NOT NULL with a
        # `server_default`, made this fire on every single authenticated
        # request via `get_current_user`'s `User` load -- this is a real,
        # pre-existing application bug, not a sandbox artifact.
        SAEnum(
            AcademicLevel,
            name="academic_level",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    # --- Added for the Real Age/Academic-Year/Gender Opportunity
    # Filtering step ---
    # `gender` -- see `Gender`'s own docstring (app/models/enums.py) for
    # the full "confirmed genuinely net-new" disclosure. Nullable,
    # self-reported, unverified -- same trust model as every other
    # profile field on this model -- and immediately settable via the
    # existing `PATCH /users/me` (see `app/schemas/user.py`'s
    # `UserProfileUpdate`), no new endpoint needed, same precedent as
    # `target_country`/`age_group`/`age`/`bio` above.
    gender: Mapped[Gender | None] = mapped_column(
        # `values_callable` -- same disclosed fix as `academic_level` above.
        SAEnum(
            Gender,
            name="gender",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    # --- Added for the User Directory Search step (Postgres Full-Text
    # Search over the user directory -- the second disclosed gap closed
    # from the Live 1:1 Video Sessions step's own "no user-search
    # endpoint" finding) ---
    #
    # `bio` -- a genuinely NEW field. This model had no free-text
    # "about me" field before this step (confirmed by reading the full
    # model before writing anything): `major`/`degree_level`/
    # `target_country` are all short, structured profile facts, not a
    # self-written blurb. The requested search vector explicitly needs a
    # "bio" component to weight at C -- rather than silently substituting
    # an existing, differently-shaped field (which would misrepresent
    # what's actually searchable) or leaving that weight tier searching
    # nothing, `bio` is added as a real, new, nullable `Text` column, same
    # "extend `User` rather than leave the feature degraded" resolution
    # already used repeatedly on this exact model for `target_country`/
    # `age_group`/`age` above. Immediately settable via the existing
    # `PATCH /users/me` (see `app/schemas/user.py`'s `UserProfileUpdate`),
    # no new endpoint needed -- same precedent as every field above it.
    bio: Mapped[str | None] = mapped_column(Text, nullable=True)

    # `search_vector` -- the generated/maintained `TSVECTOR` column
    # itself. DB-MANAGED, NOT APPLICATION-MANAGED: this column's actual
    # value is computed and kept in sync entirely by a native Postgres
    # trigger (`users_search_vector_trigger`, see migration
    # `0030_user_search_vector.py`) that fires BEFORE INSERT/UPDATE OF
    # `full_name`, `skills`, `bio` -- application code (this model, the
    # future search endpoint, `PATCH /users/me`) must NEVER assign this
    # column a value directly; whatever the trigger computes always wins,
    # since it re-derives the column unconditionally on every relevant
    # write. The column is mapped here purely so it CAN be read back (or
    # queried against with a raw `@@` match expression) from the ORM side
    # -- not so Python code can write it.
    #
    # `deferred=True` -- DISCLOSED, DELIBERATE PERFORMANCE CHOICE: without
    # this, EVERY ordinary `User` fetch in this codebase (most
    # importantly, `get_current_user`'s own per-request lookup -- this
    # runs on nearly every authenticated endpoint in the entire API) would
    # start pulling this column's full tsvector payload over the wire on
    # every single request, even though almost none of those call sites
    # have any use for it. `deferred=True` excludes `search_vector` from
    # the default `SELECT` SQLAlchemy generates for `User`; it is only
    # fetched (via one extra, explicit query) on the rare call site that
    # actually asks for it. This is also why this column's addition did
    # NOT require touching this codebase's ~16 existing raw-SQL SQLite
    # `users` test fixtures the way `bio` above did (see this step's own
    # README entry) -- a deferred column is simply never part of the
    # default column list those fixtures' ORM-level `SELECT`s need to
    # satisfy.
    search_vector: Mapped[str | None] = mapped_column(TSVECTOR, nullable=True, deferred=True)

    # `role` -- see `UserRole`'s own docstring (app/models/enums.py) for
    # the full "no role concept existed before this" disclosure. Native
    # Postgres ENUM (`native_enum=True`), NOT nullable -- every user gets
    # a real, meaningful default (`STUDENT`) at both the Python level
    # (`default=`) and the database level (`server_default=`, see
    # migration `0031_user_role`), matching this codebase's own
    # established `is_read`/`reminder_sent`-style "both defaults" posture
    # for booleans, applied here to a small enum instead. Deliberately
    # NOT used anywhere near `app/api/deps.py::require_admin` -- that
    # check stays on `is_superuser` alone; see `UserRole`'s own docstring
    # for why the two are kept as separate, non-overlapping signals.
    role: Mapped[UserRole] = mapped_column(
        # `values_callable` -- same disclosed fix as `academic_level`/`gender`
        # above. This column is NOT NULL with a `server_default` (migration
        # 0031), so unlike the two nullable columns above, this bug fired on
        # EVERY single `User` load (via `get_current_user`, i.e. nearly every
        # authenticated request in the app) before this fix -- confirmed as
        # a real crash (not a hypothesis) while booting the backend to test
        # the admin-moderation endpoints: any seeded/real user row raised
        # `LookupError: 'student' is not among the defined enum values.
        # Enum name: user_role. Possible values: STUDENT, MENTOR` the moment
        # it was loaded.
        SAEnum(
            UserRole,
            name="user_role",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=False,
        default=UserRole.STUDENT,
    )

    # `admin_role` -- Real Tiered Role System step. See `AdminRole`'s own
    # docstring (app/models/enums.py) for the full tier design and for
    # why this is NOT a repeat of the `UserRole`-vs-`is_superuser`
    # "two disagreeing signals" mistake that enum's own docstring warns
    # against: `is_superuser` remains the sole legacy `OWNER`-equivalent
    # alias, and `app/api/deps.py::_effective_admin_rank` is the ONE
    # place that resolves both signals into a single effective tier.
    # Nullable, no default -- unlike `role` above, `None` ("no elevated
    # moderation tier") is correct for the overwhelming majority of
    # users, not a placeholder needing a fallback value.
    admin_role: Mapped[AdminRole | None] = mapped_column(
        SAEnum(
            AdminRole,
            name="admin_role",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    # `role_granted_at` -- New-Volunteer Onboarding Checklist addition
    # (migration 0048). Real, disclosed gap Step 1 confirmed: nothing in
    # `assign_admin_role` (app/api/v1/endpoints/admin_roles.py) ever
    # recorded WHEN a tier was granted -- `TimestampMixin.updated_at`
    # (below) is NOT a substitute; it flips on ANY user-row update
    # (profile edits, any other admin action touching this row), not
    # specifically a role grant, so it cannot reliably answer "is this a
    # new volunteer." Set to `func.now()` the moment `admin_role` moves
    # from `None` to a real tier (a genuine first-time grant, not a
    # lateral MODERATOR<->SENIOR_MODERATOR change) and cleared back to
    # `None` on revocation -- see `assign_admin_role`'s own docstring for
    # the full reasoning, including why this also drives the onboarding
    # checklist's real reset-on-fresh-grant behavior. `None` for every
    # `is_superuser=True` legacy Owner who never passed through this real
    # endpoint -- an honest, not fabricated, absence of a real grant
    # event for those accounts.
    role_granted_at: Mapped[dt.datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    # lazy="selectin" issues a second batched query instead of a lazy-load,
    # which is the safe pattern under asyncio (a plain lazy="select" would
    # try to emit implicit I/O outside of an awaited call and raise).
    bookmarks: Mapped[list["UserBookmark"]] = relationship(
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    # --- Personalized Opportunity Intelligence + Opportunity Passport
    # step (Part A: User Opportunity Profile) ---
    #
    # Audited first (see app/models/user.py's own extensive prior-step
    # comments above): `target_country`, `age`, `academic_level`,
    # `gender`, `skills` already exist and are REUSED, not duplicated,
    # by the new eligibility engine and personalization signals below.
    # `target_country` is treated as this user's PREFERRED destination
    # country (its own pre-existing "Autonomous Scholar Agent" comment
    # already establishes that meaning) -- `residency_country` below is
    # the genuinely new, distinct concept (where the user actually
    # currently lives, a real eligibility fact, not a preference).
    #
    # Every field below is nullable, self-reported, unverified -- the
    # same trust model every other profile field on this model already
    # has -- and is settable via the existing `PATCH /users/me`
    # (`UserProfileUpdate`, app/schemas/user.py). No sensitive attribute
    # (health, religion, political belief, sexual orientation, etc.) is
    # added here; this phase's own explicit Part A instruction is to
    # capture only identity/education/language/interest/preference/goal
    # facts the user states directly, and to infer NOTHING (nationality
    # is never inferred from language, age is never inferred from
    # academic level, gender is never inferred at all).

    # IDENTITY: `nationality` is a genuinely new concept -- nothing on
    # this model previously distinguished "citizen of" from "preferred
    # destination" (`target_country`) or "currently lives in"
    # (`residency_country`, immediately below). Both are real,
    # independent eligibility facts an opportunity's own `country`/
    # `is_local_egypt` requirement can be evaluated against.
    nationality: Mapped[str | None] = mapped_column(String(120), nullable=True)
    residency_country: Mapped[str | None] = mapped_column(String(120), nullable=True)

    # LANGUAGES: plain `ARRAY(String)`, matching `skills`'s own existing
    # precedent exactly (a structured list, not a comma-separated
    # string) -- proficiency level is deliberately NOT captured (Part A's
    # own "proficiency where explicitly provided" is a real scope
    # narrowing this phase makes: no opportunity field anywhere in this
    # codebase currently states a required proficiency LEVEL, only a
    # required language itself, so there is nothing yet to compare a
    # proficiency level against -- disclosed, not silently dropped).
    languages: Mapped[list[str] | None] = mapped_column(ARRAY(String(50)), nullable=True)

    # INTERESTS: genuinely new -- the closest existing field, `skills`,
    # is a distinct concept (a competency the user HAS, not a topic the
    # user is interested IN). Free-text array, same posture as `skills`.
    interests: Mapped[list[str] | None] = mapped_column(ARRAY(String(100)), nullable=True)

    # APPLICATION GOALS: a real, closed 7-value vocabulary
    # (`ApplicationGoal`, app/models/enums.py) -- stored as a plain
    # `ARRAY(String)`, not a native Postgres enum array, since the
    # vocabulary is validated at the Pydantic schema layer
    # (`UserProfileUpdate.application_goals`) -- see that enum's own
    # docstring for the full "why not a native enum column" reasoning.
    application_goals: Mapped[list[str] | None] = mapped_column(ARRAY(String(30)), nullable=True)

    # PREFERENCES: `preferred_opportunity_types` reuses the real,
    # already-24-value `OpportunityType` enum (Taxonomy Evolution step)
    # rather than inventing a second type vocabulary -- `ARRAY` of the
    # existing native enum, same pattern `Opportunity.
    # target_academic_levels` already established for arrays of native
    # enums (`create_type=False` since the `opportunity_type` Postgres
    # enum type already exists).
    preferred_opportunity_types: Mapped[list[OpportunityType] | None] = mapped_column(
        ARRAY(
            SAEnum(
                OpportunityType,
                name="opportunity_type",
                native_enum=True,
                create_type=False,
                values_callable=lambda enum_cls: [e.value for e in enum_cls],
            )
        ),
        nullable=True,
    )
    # `remote_preference` -- `True` prefers remote/online opportunities,
    # `False` prefers in-person, `None` (default) means no stated
    # preference either way (NOT "prefers remote" -- a real 3-state
    # signal, matching this model's own established "None means
    # genuinely unknown, not a hidden default" convention).
    remote_preference: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    # Free text (e.g. "fully_funded", "partial", "any") rather than an FK
    # to `funding_intelligence.FundingType` -- that enum lives in a
    # service module, not `app.models`, and importing a service enum
    # into a model would invert this codebase's own layering; this
    # column's values are a documented convention, not DB-enforced.
    funding_preference: Mapped[str | None] = mapped_column(String(30), nullable=True)
    # How many days of lead time before a deadline the user wants to
    # still see it recommended (their own stated "deadline tolerance") --
    # genuinely new; nothing on this model previously captured a
    # deadline-proximity preference at all.
    deadline_lead_time_days: Mapped[int | None] = mapped_column(Integer, nullable=True)

    # --- APEX Onboarding Production Integration step ---
    #
    # `date_of_birth` -- genuinely new. The pre-existing `age` (Integer,
    # above) is a once-entered snapshot that goes stale every year and
    # cannot answer "is this user 13 on a specific future date" the way
    # a scholarship's own age-cutoff rule needs. The onboarding directive
    # explicitly requires the EXACT date of birth as the authoritative
    # source of age, with age derived deterministically from it (see
    # `app/services/age.py`), not trusted as a stored integer. `age`
    # itself is left in place, unmodified, for any existing caller that
    # already reads it -- this is an additive, backward-compatible
    # extension, not a replacement. Never exposed through any public
    # profile/search view (see `app/services/privacy_service.py` --
    # `ProfileViewOut`/`PublicSearchProfileOut` have no DOB field).
    date_of_birth: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # `english_level` -- see `EnglishLevel`'s own docstring
    # (app/models/enums.py) for the full "confirmed genuinely missing"
    # disclosure. Native Postgres ENUM, same `values_callable` fix as
    # `academic_level`/`gender` above.
    english_level: Mapped["EnglishLevel | None"] = mapped_column(
        SAEnum(
            EnglishLevel,
            name="english_level",
            native_enum=True,
            values_callable=lambda enum_cls: [e.value for e in enum_cls],
        ),
        nullable=True,
    )

    # `other_nationalities` -- the pre-existing `nationality` (String,
    # above) remains the single PRIMARY nationality every existing
    # caller (`eligibility_engine.py`, `user_intelligence.py`,
    # `privacy_service.py`) already reads and keeps reading unchanged.
    # The onboarding UI's own approved design lets a user add more than
    # one nationality as chips -- persisting only the first and silently
    # dropping the rest would discard real, explicitly provided user
    # data, which this program's own standing rules forbid. Rather than
    # replace `nationality` or build a second nationality system, this
    # is the smallest backward-compatible extension: an additional
    # nullable array holding any nationalities BEYOND the primary one,
    # same `ARRAY(String)` posture as `languages`/`skills`. No existing
    # code reads this column yet -- it exists so the data is preserved,
    # available the moment any future eligibility/personalization rule
    # needs more than one nationality.
    other_nationalities: Mapped[list[str] | None] = mapped_column(
        ARRAY(String(120)), nullable=True
    )

    # `onboarding_completed_at` -- the single real, server-set source of
    # truth for "has this user actually finished onboarding," set once
    # (and only once) by `POST /users/me/onboarding/complete`
    # (app/api/v1/endpoints/user_onboarding.py) the moment that endpoint's
    # explicit-field persistence actually commits -- never trusted from
    # a client-supplied flag (this program's own standing security rule).
    # `None` for every user who has never completed it, including every
    # account that existed before this step. A second call to that same
    # endpoint (refresh/retry/back-button) leaves this timestamp
    # unchanged -- idempotent by construction, not by a separate dedup
    # table.
    onboarding_completed_at: Mapped[dt.datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # lazy="selectin" issues a second batched query instead of a lazy-load,
    # which is the safe pattern under asyncio (a plain lazy="select" would
    # try to emit implicit I/O outside of an awaited call and raise).
    bookmarks: Mapped[list["UserBookmark"]] = relationship(
        back_populates="user",
        lazy="selectin",
        cascade="all, delete-orphan",
    )

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"
