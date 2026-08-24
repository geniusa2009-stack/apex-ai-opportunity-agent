import enum


class OpportunityType(str, enum.Enum):
    """What kind of opportunity this record represents."""

    SCHOLARSHIP = "scholarship"
    INTERNSHIP = "internship"
    VOLUNTEER = "volunteer"
    COURSE = "course"


class BookmarkStatus(str, enum.Enum):
    """Where a user is in their journey with a saved opportunity.

    Only `saved` / `applied` are required right now. Extending this later
    (e.g. `withdrawn`, `accepted`, `rejected`) is additive and just needs
    a migration to alter the Postgres enum type.
    """

    SAVED = "saved"
    APPLIED = "applied"


class TrackerStatus(str, enum.Enum):
    """Kanban column a tracked scholarship application sits in
    (Ecosystem Feature 1: AI-Powered Application Kanban Tracker).

    Unlike OpportunityType/BookmarkStatus above, `ApplicationTracker.status`
    (app/models/application_tracker.py) is NOT mapped as a native Postgres
    enum -- that step's ask specified the column type as plain `String`,
    distinct from how the Enum-typed columns above were asked for. This
    enum is still the single source of truth for the allowed values, just
    enforced at the Pydantic layer (app/schemas/application_tracker.py's
    `TrackerStatusUpdate`) instead of a DB-level type.
    """

    INTERESTED = "interested"
    PREPARING_DOCS = "preparing_docs"
    APPLIED = "applied"
    INTERVIEWING = "interviewing"
    ACCEPTED = "accepted"
    REJECTED = "rejected"


class FocusSessionStatus(str, enum.Enum):
    """Lifecycle state of a `FocusSession` (Ecosystem Feature 5: AI
    Multi-Sensory Study Buddy & Distraction Shield -- upgraded, on top of
    a reconstructed "AI Pomodoro & Focus Rooms" baseline).

    This enum, the `status` column it backs, and in fact the entire
    `FocusSession` model this session lives on top of are this project's
    OWN reconstruction of a base feature ("Ecosystem Feature 5") that the
    upgrade step's ask referenced as already existing (e.g. "keep the
    previous fields") but which was confirmed, via a full repo search, to
    never have actually been built. See app/models/focus_room.py's module
    docstring and the README's "Ecosystem Feature 5" section for the full
    disclosure on this. Same plain-String-column-backed-by-a-Python-enum
    posture as `TrackerStatus` above, for the same reason: this project's
    convention (once a feature's ask specifies a status column without
    explicitly asking for a native Postgres ENUM type) is to keep it a
    plain String enforced at the Pydantic layer, which gives cheap,
    migration-free extensibility if new states are ever needed.
    """

    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    ABANDONED = "abandoned"


class VideoProcessingStatus(str, enum.Enum):
    """Lifecycle state of a `Lesson`'s HLS transcode + IPFS pinning
    pipeline (Core Video Media Processing Pipeline, on top of Ecosystem
    Features 13 & 14's LMS & Interactive Academy).

    Same plain-String-column-backed-by-a-Python-enum posture as
    `TrackerStatus`/`FocusSessionStatus` above -- this ask specifies the
    enum's allowed values directly but doesn't say to map `Lesson.video_processing_status`
    as a native Postgres ENUM type, so it stays a plain String column
    (see app/models/courses.py), enforced at the Pydantic layer instead.
    `None` (the column is nullable) is a distinct fifth state meaning "no
    video has ever been uploaded for this lesson" -- a text-only lesson
    should never show `PENDING` forever just because it has no video.
    """

    PENDING = "pending"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class StudentVideoStatus(str, enum.Enum):
    """Lifecycle state of a `StudentVideo` (Student Video Profiles &
    AI-Moderated Upload System).

    Disclosed file-placement deviation: that step's ask names this enum
    directly under its "DATABASE MODELS & MIGRATION (`app/models/
    student_videos.py`)" component, implying it might live in that same
    file. It's defined here instead, alongside `TrackerStatus`/
    `FocusSessionStatus`/`VideoProcessingStatus` -- every other
    status-lifecycle enum in this codebase, no exceptions -- and imported
    into `app/models/student_videos.py`. Splitting this one enum out to
    its own model file while every other one lives centrally here would
    be a real, avoidable inconsistency for a purely cosmetic difference;
    `app/models/student_videos.py` still fully "includes" it via that
    import, which is what actually matters for the model to work.

    Same plain-String-column-backed-by-a-Python-enum posture as every
    enum above, for the same reason: this ask writes "StudentVideoStatus
    Enum" without asking for a native Postgres ENUM TYPE specifically, so
    `StudentVideo.status` stays a plain String column (see
    app/models/student_videos.py), enforced at the Pydantic layer.

    `PENDING_AI` is the initial state (a video was just uploaded and is
    queued for/undergoing the async pipeline); `APPROVED`/`REJECTED` are
    Gemini's own safety-evaluation outcomes; `FAILED` means the pipeline
    itself broke (an FFmpeg, Whisper, Gemini, or Pinata call failed on
    every retry attempt) -- distinct from `REJECTED`, which means the
    pipeline ran successfully and Gemini genuinely judged the content
    unsafe/inappropriate. Unlike `VideoProcessingStatus`, there is no
    fifth `None` "never processed" state here -- every `StudentVideo` row
    is created already queued for processing (see
    `student_videos.py`'s router), so `PENDING_AI` is a real, meaningful
    default, not a nullable "N/A" sentinel.
    """

    PENDING_AI = "pending_ai"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"


class VideoType(str, enum.Enum):
    """Which of this codebase's two independent video pipelines a
    `VideoPlaybackLog` row's `video_id` points into (Video Analytics,
    Watch Time Tracking & Engagement Engine).

    Disclosed deliberate deviation from every other enum in this file:
    every status-lifecycle enum above uses lowercase wire values
    (`"pending_ai"`, `"in_progress"`, ...), but this milestone's own ask
    is explicit and literal about the wire shape it wants -- `"video_type":
    str` with `'UGC' | 'COURSE'`, uppercase, in its own heartbeat payload
    spec. Honored exactly as asked, same posture this codebase already
    takes elsewhere when an ask is explicit about a literal value
    (`AdminOverrideRequest.target_status`'s own docstring makes the same
    "ask says something specific, that wins over the ambient convention"
    call).

    `UGC` maps to `StudentVideo` (`app/models/student_videos.py`,
    `app/models/analytics.py`'s own module docstring); `COURSE` maps to
    `Lesson` (`app/models/courses.py`). `VideoPlaybackLog.video_id` is a
    plain `UUID` column with NO foreign key to either table -- see that
    model's own docstring for why a single polymorphic id column (exactly
    as this milestone's own ask specifies) can't carry a real FK
    constraint, and how the router resolves it into the correct table at
    the application layer instead, keyed off this enum's value.
    """

    UGC = "UGC"
    COURSE = "COURSE"


class AcademicLevel(str, enum.Enum):
    """A student's current stage in the Egyptian education system
    (Backend Folder Structure & Core Database Models step -- that ask's
    own literal enumeration: "1st/2nd/3rd Preparatory, 1st/2nd/3rd
    Secondary, University 1st-6th Year, Masters, PhD").

    Lives on `User.academic_level` (`app/models/user.py`) and backs
    `Opportunity.target_academic_levels` (`app/models/opportunity.py`,
    an ARRAY of this enum) for the "strict filtering" that ask names --
    an opportunity whose `target_academic_levels` includes a student's
    own `academic_level` (or leaves it unset entirely -- broadly
    applicable) is a genuine match; one that names OTHER levels only is
    not, a materially stricter signal than the pre-existing free-text
    `User.age_group` / `Opportunity.target_age_group` pair (three
    coarse buckets: `'prep_secondary'`, `'university'`, `'graduates'').

    DISCLOSED, DELIBERATE NON-REPLACEMENT of `age_group`/`target_age_group`:
    those two fields are additive, not superseded -- both already exist,
    are already wired through `PATCH /users/me` and the Student
    Dashboard's opportunity-recommendation query, and are covered by
    passing tests. `academic_level` is a stricter, complementary signal
    layered alongside them (see `dashboards.py`'s own updated query),
    not a rename/migration of the older field. A future pass could
    decide to deprecate `age_group` in favor of this enum once every
    opportunity in the system has been backfilled with real
    `target_academic_levels` data -- not done here, since no such
    backfill mechanism exists yet and the coarser field still works.

    Native Postgres ENUM type (`native_enum=True`, unlike most of the
    plain-String-backed enums elsewhere in this file) -- matches
    `OpportunityType`'s own precedent immediately above/below in this
    file (a category/classification enum with a small, stable, unlikely-
    to-change-often value set), not the lifecycle-status enums' looser
    posture.
    """

    PREP_1 = "prep_1"
    PREP_2 = "prep_2"
    PREP_3 = "prep_3"
    SECONDARY_1 = "secondary_1"
    SECONDARY_2 = "secondary_2"
    SECONDARY_3 = "secondary_3"
    UNIVERSITY_YEAR_1 = "university_year_1"
    UNIVERSITY_YEAR_2 = "university_year_2"
    UNIVERSITY_YEAR_3 = "university_year_3"
    UNIVERSITY_YEAR_4 = "university_year_4"
    UNIVERSITY_YEAR_5 = "university_year_5"
    UNIVERSITY_YEAR_6 = "university_year_6"
    MASTERS = "masters"
    PHD = "phd"


class Gender(str, enum.Enum):
    """A student's self-reported gender (Real Age/Academic-Year/Gender
    Opportunity Filtering step).

    DISCLOSED, NECESSARY ADDITION: confirmed via a full-repo grep
    (`grep -rn -i gender app/`) that no field of this name, or concept
    like it, existed anywhere on `User` or `Opportunity` before this --
    genuinely net-new, not a rename or an extension of something that
    already existed (unlike `age`/`academic_level`, which this same step
    confirmed were already real and already wired through
    `PATCH /users/me`).

    Lives on `User.gender` (app/models/user.py) and
    `Opportunity.target_gender` (app/models/opportunity.py, single value
    -- NOT an array, unlike `target_academic_levels` -- an opportunity's
    gender restriction, when it has one at all, is exactly one of these
    two values, never "male AND female," which would just be
    "unrestricted," i.e. `NULL`). Nullable everywhere it's used, same
    self-reported/unverified trust model as every other `User` profile
    field on this codebase (`academic_level`, `target_country`, ...) --
    a user who hasn't set this is simply excluded from any gender-based
    filter, never defaulted to a guess.

    Two values, not more: this platform's own real, currently-scraped
    opportunity eligibility language (Egyptian scholarship/internship
    postings) only ever restricts by a binary male/female distinction
    when it restricts at all -- confirmed via `aggregator_service.py`'s
    own extraction prompt and a scraper-pipeline read before adding this
    field, which found no third category referenced anywhere in this
    codebase's real data model. A future step can extend this enum
    (an additive Postgres `ALTER TYPE ... ADD VALUE` migration) if a
    real need for more values is ever found -- not invented speculatively
    here.

    Native Postgres ENUM type (`native_enum=True`) -- matches
    `AcademicLevel`/`UserRole`'s own precedent (a small, stable
    classification enum), not the lifecycle-status enums' looser
    plain-String posture.
    """

    MALE = "male"
    FEMALE = "female"


class MentorshipStatus(str, enum.Enum):
    """Lifecycle state of a `MentorshipRelation` (Hyper-Personalized
    Platform step's "Mentorship Relation Model (Maturity 7)").

    Plain-String-backed, not a native Postgres ENUM -- see
    `app.models.features.MentorshipRelation`'s own docstring for the full
    "this ask didn't explicitly request a native enum type" reasoning,
    same posture as `TrackerStatus`/`FocusSessionStatus` above.

    `PENDING` is the initial state (a mentee has requested, or a mentor
    has offered -- see the `/mentorship` router's own docstring for which
    direction actually exists in this pass); `ACTIVE` once the other party
    accepts; `COMPLETED` once either party ends it. Exactly the three
    values this step's own ask names, no more (see
    `MentorshipRelation.__table_args__`'s own docstring on why a
    `REJECTED` state, though a natural addition, was deliberately left
    out of THIS pass).
    """

    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"


class SourceType(str, enum.Enum):
    """Which platform a `Source` row's content is actually fetched from
    (Hybrid Opportunity Aggregation Engine step).

    Lowercase wire values -- follows the MAJORITY convention of every
    lifecycle/category enum in this file (`OpportunityType`, `BookmarkStatus`,
    `TrackerStatus`, ...), NOT `VideoType`'s deliberate uppercase exception
    above. `VideoType` went uppercase because that milestone's own ask was
    explicit and literal about the exact wire shape (`'UGC' | 'COURSE'`)
    -- this milestone's ask gives `"Enum: 'TELEGRAM', 'FACEBOOK', 'WEBSITE'"`
    in all-caps prose, but that's ordinary enum-naming shorthand in the
    ask's own writing (the same way `StudentVideoStatus`'s ask wrote
    `PENDING_AI`/`APPROVED` in caps yet that enum's real values are
    lowercase), not a literal wire-format instruction the way `VideoType`'s
    ask explicitly was. Lowercase was judged the better default here.

    Lives on `Source` (app/models/source.py), NOT `Opportunity` -- see
    that model's own module docstring for why a `source_type`/`source_name`
    pair was NOT duplicated onto `Opportunity` itself despite this
    milestone's ask listing them as `Opportunity` fields.
    """

    TELEGRAM = "telegram"
    FACEBOOK = "facebook"
    WEBSITE = "website"


class LiveSessionStatus(str, enum.Enum):
    """Lifecycle state of a `LiveSession` (real-time WebRTC video call
    booking, `app/models/live_session.py`).

    Plain-String-backed, not a native Postgres ENUM -- same posture as
    `MentorshipStatus`/`TrackerStatus`/`FocusSessionStatus` above (this
    codebase's own established default for lifecycle-status columns
    unless a step explicitly asks for a native enum type).

    `REQUESTED` -- the initial state, set the moment `requested_by`
    books a proposed time with `requested_with`. `CONFIRMED` once
    `requested_with` accepts (the only status from which the WebRTC
    signaling WebSocket will actually admit a connection -- see
    `live_session_service.assert_can_join`). `DECLINED` if
    `requested_with` rejects instead. `CANCELLED` if either party backs
    out after requesting/confirming but before the call happens.
    `COMPLETED` once either participant marks the session done.

    Five values, one more than `MentorshipStatus`'s three -- unlike that
    model (whose ask explicitly named exactly three states, with
    "reject" deliberately modeled as row deletion instead of a fourth
    stored value), a live video session is a real-time, time-boxed
    booking with no equivalent ask to defer to, so `DECLINED` and
    `CANCELLED` are both kept as real, distinct, queryable states here
    -- a mentor's "no-thanks" history and a "we had to cancel" history
    are genuinely different facts about a session, both worth being able
    to list/filter on later, unlike a rejected mentorship request (which
    this codebase's own precedent already treats as not worth keeping a
    row for at all).
    """

    REQUESTED = "requested"
    CONFIRMED = "confirmed"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    COMPLETED = "completed"


class UserRole(str, enum.Enum):
    """A user's self-reported directory role (User Directory Search step
    -- the `role` filter that step's own ask named, "e.g., mentor,
    student").

    DISCLOSED, NECESSARY ADDITION: no role concept of any kind existed
    anywhere on `User` before this (confirmed by reading the full model
    and grepping the whole codebase for "role"/"Role" before writing
    anything) -- `is_superuser` is the only existing role-adjacent
    signal, and it answers a different question ("can this account
    perform admin actions") than "what kind of directory participant is
    this" does. Rather than either silently reusing `is_superuser` for a
    purpose it wasn't designed for, or shipping a `role` query parameter
    that filters against a column that doesn't exist, this is a real,
    new, honestly-disclosed column -- same "extend `User` rather than
    leave a named requirement degraded" resolution already used
    repeatedly on this exact model (`target_country`, `age_group`,
    `bio`).

    Deliberately does NOT include an `ADMIN` member. `is_superuser`
    already IS this platform's single source of truth for admin
    authorization (`app/api/deps.py::require_admin`) -- adding a second,
    independent "admin" signal here would create two places that could
    disagree about whether a given user is an admin, for no real
    benefit, and the ask's own illustrative example ("mentor, student")
    only ever names these two.

    `STUDENT` is the default (`server_default` in migration
    `0031_user_role`) -- this is fundamentally a student-facing
    platform; an unset role should mean "an ordinary student," not an
    ambiguous NULL every directory search would need to special-case.
    Self-reported, like every other profile field on this model
    (`academic_level`, `target_country`, ...) -- nothing currently
    promotes a user to `MENTOR` automatically (e.g. on their first
    accepted `MentorshipRelation`); settable via the existing
    `PATCH /users/me`, same precedent as every field above it. A future
    step could add real verification/auto-promotion; not built here,
    disclosed as an open gap rather than silently assumed.

    Native Postgres ENUM type (`native_enum=True`) -- matches
    `AcademicLevel`/`OpportunityType`'s own precedent (a small, stable,
    unlikely-to-change-often classification enum), not the
    lifecycle-status enums' looser plain-String posture.
    """

    STUDENT = "student"
    MENTOR = "mentor"


class AdminRole(str, enum.Enum):
    """Real, ordered moderation/admin tier (Real Tiered Role System step),
    replacing the "everyone is `is_superuser=True` or nobody" all-or-
    nothing posture that the live admin-moderation endpoint test (the
    step immediately before this one) confirmed was the ONLY admin
    signal in the codebase, with zero writable API to grant it.

    DISCLOSED, IMPORTANT DISTINCTION from `UserRole` immediately above --
    that enum's own docstring explains it deliberately has no `ADMIN`
    member specifically to avoid creating "two places that could
    disagree about whether a given user is an admin." This enum is NOT
    that mistake repeated: it does not compete with `is_superuser` as an
    independent yes/no admin signal. Instead, exactly one resolution
    rule governs both signals together everywhere this enum is checked
    (`app/api/deps.py`'s `_effective_admin_rank`): a user's effective
    tier is `OWNER` if EITHER `is_superuser` is `True` OR `admin_role`
    is `OWNER`, otherwise it's whatever `admin_role` says (or "no tier"
    if `admin_role` is `None`). `is_superuser` is treated as a legacy/
    backward-compatible ALIAS for `OWNER`, not a second, independently-
    checked gate -- so an already-`is_superuser=True` account (there is
    exactly one path to becoming one today: direct DB access) keeps
    working exactly as before, with no regression, while every NEW grant
    going forward happens through this real, tiered, auditable column
    instead.

    Three tiers, ordered low to high (see `.rank` below -- a real
    ordering, not just three more booleans):

    - `MODERATOR` -- the entry tier for a new volunteer. Real moderation
      actions that are reviewable/reversible: view the review queue,
      approve/reject a flagged video (`override_video_decision` -- can
      always be called again to correct an earlier decision), view the
      admin dashboard, lift a chat ban early. Deliberately does NOT get
      the two actions that permanently destroy data with no undo path
      (`DELETE /admin/videos/{id}`, `POST .../reject-and-purge-media` --
      both trigger real, irreversible R2/storage deletion via
      `task_delete_orphaned_media`, on top of the row mutation/deletion
      itself). This mirrors ordinary trust-and-safety practice: broad
      day-to-day moderation judgment calls are fine to delegate widely
      and immediately; irreversible destructive actions are not, until
      someone has more standing.
    - `SENIOR_MODERATOR` -- everything `MODERATOR` can do, PLUS the two
      destructive actions above. Still cannot grant/revoke roles for
      other users -- that stays `OWNER`-only, per this step's own
      explicit design.
    - `OWNER` -- everything `SENIOR_MODERATOR` can do, plus role
      management (`PATCH /admin/users/{user_id}/role`,
      `GET /admin/team`). NOT grantable through that same role-management
      endpoint (see its own docstring) -- the very first `OWNER` for a
      deployment is a genuine bootstrapping problem (nobody exists yet
      who could grant it through an API gated on already having it), so
      it is deliberately a one-time, disclosed, direct-DB/script step
      (`scripts/bootstrap_first_owner.py`), not new API surface built for
      something that happens once per deployment.

    Nullable on `User` (`admin_role`, unlike `UserRole.role`'s NOT NULL
    `STUDENT` default) -- `None` is a real, common, correct state ("no
    elevated moderation tier"), not an omission needing a fallback
    value; nearly every user will correctly have `admin_role IS NULL`
    forever.

    Native Postgres ENUM type (`native_enum=True`) -- same "small,
    stable, unlikely-to-change-often classification enum" precedent as
    `UserRole`/`AcademicLevel` immediately above, not the looser
    lifecycle-status posture. Uses the same disclosed `values_callable`
    fix (`app/models/user.py`) already applied to `UserRole`/
    `AcademicLevel`/`Gender` in the immediately preceding step, from the
    start -- this enum was never affected by that bug in the first
    place, by construction.
    """

    MODERATOR = "moderator"
    SENIOR_MODERATOR = "senior_moderator"
    OWNER = "owner"

    @property
    def rank(self) -> int:
        """Higher is more privileged. Used for real ">=this tier" checks
        (`app/api/deps.py`) instead of separate booleans per action."""
        return {
            AdminRole.MODERATOR: 1,
            AdminRole.SENIOR_MODERATOR: 2,
            AdminRole.OWNER: 3,
        }[self]


class ModerationAction(str, enum.Enum):
    """Automatic Moderation Activity Log step -- the closed set of
    actions that write a real `ModerationLogEntry`
    (app/models/moderation_log.py).

    Deliberately covers exactly the four real WRITE actions among the
    six routes the Real Tiered Role System step confirmed and tiered --
    `GET /admin/videos/review-queue` and `GET /dashboards/admin` are
    both read-only and excluded per this step's own literal "review-
    queue-view doesn't need logging since it's read-only" instruction.

    NEXT ADMIN ACTION, READ THIS: any FUTURE write-action route gated on
    `RequireModerationAccess`/`RequireSeniorModerationAccess`/`RequireOwner`
    should add its own member here, then call
    `moderation_log_service.log_action(...)` at its own real mutation
    site -- same pattern as the four call sites already wired (see each
    one's own "Automatic Moderation Activity Log" comment in
    `admin_moderation.py`/`chat.py`). This is a real, ordinary enum
    member addition, NOT a decorator or a separate registration step --
    intentionally, so a future action can't silently "forget" logging by
    missing an unrelated wiring step; the only way to add a new logged
    action is to touch this file AND the route's own body together.
    """

    VIDEO_OVERRIDE = "video_override"
    VIDEO_DELETE = "video_delete"
    VIDEO_REJECT_AND_PURGE = "video_reject_and_purge"
    CHAT_UNBAN = "chat_unban"

    # --- Extend Moderation to Chat Messages addition (migration 0040) ---
    # Three new real WRITE actions on a `ChatMessageReport` case -- see
    # `app/api/v1/endpoints/admin_chat_reports.py`'s own module docstring
    # for the full "second queue" reasoning and each route's own tier
    # decision. Same "NEXT ADMIN ACTION" discipline as above: a plain enum
    # member addition, paired with its own real `log_action(...)` call
    # site, nothing more.
    CHAT_REPORT_DISMISS = "chat_report_dismiss"
    CHAT_MESSAGE_DELETE = "chat_message_delete"
    CHAT_REPORT_ESCALATE_BAN = "chat_report_escalate_ban"

    # --- Repeat-Offender Detection addition (migration 0041) ---
    # The automatic Arabic-filter ban (`app/api/v1/endpoints/chat.py`'s
    # WebSocket handler, `chat_security.ban_user` call site) previously
    # wrote NO log entry at all -- confirmed, real gap: a Redis TTL ban
    # self-deletes on expiry with no durable trace, so a real "how many
    # times has this user been banned" count silently undercounted every
    # auto-ban that had already expired by the time anyone asked. This
    # member, paired with a real new `log_action(...)` call at that exact
    # site, closes it -- `target_type=USER`/`target_id=<banned user>`,
    # same direct-target convention `CHAT_UNBAN` already uses (no
    # `outcome` JSON extraction needed to recover the real offender).
    CHAT_AUTO_BAN = "chat_auto_ban"


class ModerationTargetType(str, enum.Enum):
    """What kind of real row a `ModerationLogEntry.target_id` refers to
    -- `target_id` itself is deliberately NOT a foreign key (see that
    column's own docstring for why), so this discriminator is what makes
    a stored id unambiguous across the two real target kinds this step's
    four actions ever touch.

    ALSO reused, unmodified, as the `{item_type}` path-segment discriminator
    for the Claim System's own `_CLAIMABLE_MODELS` dispatch table
    (`app/api/v1/endpoints/admin_claims.py`) -- that module's own docstring
    explains why a genuinely new claimable queue is meant to be a new
    member here, not a new pair of endpoints. `CHAT_MESSAGE_REPORT` below
    is the first real use of that extension point.
    """

    STUDENT_VIDEO = "student_video"
    USER = "user"
    # --- Extend Moderation to Chat Messages addition (migration 0040) ---
    # The reported-message "case" itself (`ChatMessageReport`, one open
    # row per reported `ChatMessage`) -- NOT `ChatMessage` directly, and
    # NOT one member per individual report submission. See
    # `app/models/chat_moderation.py`'s own module docstring for the full
    # "what is a case, this time" reasoning, mirroring Phase 4's own
    # `admin_claims.py` Step 1 investigation for `StudentVideo`.
    CHAT_MESSAGE_REPORT = "chat_message_report"


class ChatReportReason(str, enum.Enum):
    """The closed set of reasons a user may cite when reporting a chat
    message (`POST /chat/rooms/{room_id}/messages/{message_id}/report`).
    Plain `str` enum, same posture as every other closed-set classification
    in this codebase (`OpportunityType`, `StudentVideoStatus`) -- native
    Postgres enum on the `chat_message_report_entries.reason` column (see
    that model), matching `ChatReportReason`'s own "small, stable,
    unlikely-to-change" precedent (`AdminRole`/`UserRole`), not the looser
    lifecycle-status posture `StudentVideoStatus` deliberately opts out of.

    Four categories -- the task's own literal "spam, harassment,
    inappropriate content, etc." examples, plus a real `OTHER` escape
    hatch with a free-text `details` field alongside it (see
    `ChatMessageReportEntry.details`) for a real reason this closed set
    doesn't anticipate, rather than forcing every edge case into the
    nearest-fit bucket and losing the reporter's actual context.
    """

    SPAM = "spam"
    HARASSMENT = "harassment"
    INAPPROPRIATE_CONTENT = "inappropriate_content"
    OTHER = "other"
