"""Training / Practice Mode for New Volunteers -- database schema +
the real, centrally defined practice-case bank.

STEP 1 INVESTIGATION (this task's own required first step):

- Re-read `app/models/onboarding.py` (Feature 9) in full. Its own module
  docstring EXPLICITLY calls out this exact feature as future, separate
  work: "explicitly NOT a simulated practice environment (that is a
  separate, later, deliberately distinct feature on the remaining-items
  list: 'training/practice mode for new volunteers')." That confirms
  two things directly from the real prior-phase source, not by
  assumption: (a) onboarding IS the natural place to hook a completion
  signal once this feature exists, and (b) onboarding's own five steps
  are self-reported orientation clicks ("go look at X real view"), not
  action-taking -- a genuinely different shape of interaction from what
  this task asks for (a volunteer actually DECIDES an outcome on a
  case). So this task hooks INTO onboarding (a real, new 6th step) but
  does not rebuild onboarding's own five steps' self-report shape for
  itself -- see the new 6th step's own docstring below for why it is
  server-computed, not self-reported like the other five.
- Checked every existing claimable/reviewable item type (`StudentVideo`,
  `ChatMessageReport`, `GuildContentReport`, `Opportunity`,
  `ModerationAppeal` -- the real `admin_claims._CLAIMABLE_MODELS`
  dispatch table) for any existing "fake"/"test"/"practice"/"sandbox"
  concept: none exists anywhere in this codebase (grepped
  `is_practice`/`is_test`/`is_fake`/`is_sandbox`/`practice_mode`/
  `sandbox` across `app/`; every real hit is either this coding
  environment's own sandbox or an unrelated `is_test_mode`-shaped
  string in an unrelated service). This is being built from scratch.

STEP 2 DESIGN AND JUSTIFICATION (this task's own required explicit
scope decision):

SCOPE DECISION 1 -- rehearsal-only, NOT graded evaluation. A real
scoring/answer-key system would require a genuine "correct" outcome
per case -- but real content-moderation decisions (dismiss vs. escalate
vs. delete a borderline report) are frequently legitimate JUDGMENT
CALLS a small real team's own Senior Moderators/Owner may reasonably
disagree on, not objectively fact-checkable answers. Building and
maintaining a real answer key would either (a) flatten genuinely
judgment-call cases into a single "correct" choice, quietly teaching
new volunteers a fabricated certainty this codebase's own real
moderation practice does not have, or (b) require a much larger real
investment (multiple acceptable answers per case, partial-credit
rules, real reviewer sign-off on the key itself) that is not justified
for what a SMALL team actually needs right now: a safe place to
rehearse the real mechanics (claim-equivalent flow, the real tier
limits, the real UI) without fear of a real consequence. This mirrors
`app/models/onboarding.py`'s own established posture one step further
-- that feature already chose "self-reported orientation," not a
graded quiz, for the same small-team-pragmatism reason. Real,
non-graded completion tracking (Step 3.2) is still genuinely useful and
is built with full rigor; a real scoring engine is not built. This
can be revisited later if a real, concrete pedagogical need for scoring
emerges -- nothing here forecloses adding it.

SCOPE DECISION 2 -- a small, hand-written, centrally defined case bank
(`PRACTICE_CASES` below), NOT synthetic/generated cases. Mirrors
`ONBOARDING_STEPS`' own exact shape (a frozen dataclass tuple, server-
defined, English content with the frontend owning bilingual copy via
its own label map -- see that module's own docstring for why). A small
number of hand-written, realistic-but-obviously-fake scenarios is more
honest (a human deliberately chose what to teach) and more maintainable
for a small team (editing a python tuple, no generation pipeline, no
Gemini/LLM dependency, no risk of a generator producing something
inappropriate or unrealistic) than building real generation logic for
what amounts to a handful of cases.

SCOPE DECISION 3 -- covers the three item types with real, user-facing
CONSEQUENCES (a real notification, a real ban, real content deletion):
video review, chat-message reports, and guild-content reports.
Deliberately EXCLUDES `Opportunity` review: approving/rejecting a
scraped listing has no real notification/ban/deletion side effect on a
real person at all (see `admin_opportunities.py`'s own module docstring:
"nothing here is irreversibly destructive") -- it is an editorial
decision, not a trust-and-safety one, so it is outside this task's own
explicit safety framing ("no real notifications sent to real users, no
real bans applied to real accounts, no real content deleted").

STEP 3.1 DESIGN, THE AIRTIGHT SAFETY BOUNDARY -- this task's own
explicit top priority ("this safety guarantee is the one part of this
feature that must be built with full rigor regardless of how the rest
is scoped"). The literal ask names "a real `is_practice` flag or
equivalent, checked everywhere a real action would normally have a
real side effect." Two real designs were weighed:

  (a) Insert fake rows into the REAL `student_videos`/
      `chat_message_reports`/`guild_content_reports` tables with a new
      `is_practice` column, and thread a real check through every real
      action route (`delete_video_permanently`, `resolve_chat_report`,
      `dismiss_guild_report`, etc.) to skip real side effects for a
      practice row.
  (b) A wholly SEPARATE model (this file) that never shares a table,
      a row, or a mutation code path with any real item type at all.

(a) was rejected: the real surface area touching those three tables is
large and growing (review queues, claim system, high-risk view,
dashboards, timeline/activity log, CSV export, moderation stats, the
Two-Person Confirmation for Dangerous Actions gate this task's own
prior phase just added) -- EVERY one of those real queries/paths would
need to remember to exclude `is_practice=True` rows forever, including
every FUTURE feature added to this codebase. A single missed filter
anywhere is a real production bug: a fake row polluting a real queue
count, a real Owner-facing stats dashboard, or worse, a real action
route that forgot to check the flag actually executing a real side
effect against fake data. That is the opposite of "airtight."

(b) is what this file builds, and IS the "equivalent" the task's own
wording explicitly allows for: `PracticeCase` here is not an ORM model
backed by a table at all (same "small, fixed, hand-authored list" shape
as `ONBOARDING_STEPS` -- a frozen dataclass tuple, not a DB table,
since this content is centrally authored and never needs runtime CRUD).
The only real DB table this feature adds, `PracticeCaseAttempt` below,
records ONLY which practice case a volunteer acted on and what choice
they made -- it has no foreign key to, and no code path into,
`StudentVideo`/`ChatMessageReport`/`GuildContentReport`/
`ModerationLogEntry`/`Notification`/the chat-ban Redis store/R2 storage
at all. `app/services/practice_case_service.py`'s own module docstring
states this explicitly and is the one place Step 4's "prove the
boundary holds" tests target directly: a practice action is
STRUCTURALLY INCAPABLE of reaching any real mutation function, not
merely gated by a flag a future edit could forget to check.
"""

import datetime as dt
import uuid
from dataclasses import dataclass

from sqlalchemy import DateTime, ForeignKey, Index, String, Text, UniqueConstraint, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base
from app.models.enums import AdminRole

# Real, closed, three-value set -- deliberately NOT `ModerationTargetType`
# (see this module's own docstring, STEP 3.1): a practice case is never
# a real moderation target, and must never be dispatchable through
# `admin_claims._CLAIMABLE_MODELS` or any other real item_type table.
PRACTICE_CASE_TYPE_VIDEO_REVIEW = "video_review"
PRACTICE_CASE_TYPE_CHAT_REPORT = "chat_report"
PRACTICE_CASE_TYPE_GUILD_REPORT = "guild_report"
PRACTICE_CASE_TYPES: frozenset[str] = frozenset(
    {PRACTICE_CASE_TYPE_VIDEO_REVIEW, PRACTICE_CASE_TYPE_CHAT_REPORT, PRACTICE_CASE_TYPE_GUILD_REPORT}
)

# Real actions a volunteer may choose per practice case type -- mirrors
# the REAL tier decisions this codebase already made for the real
# routes these cases rehearse (`admin_moderation.py`'s `RequireModerationAccess`
# vs `RequireSeniorModerationAccess` split for video override/delete;
# `admin_chat_reports.py`'s "dismiss/escalate-ban = Moderator,
# delete-message = Senior Moderator"; `admin_guild_reports.py`'s
# "dismiss = Moderator, delete-content = Senior Moderator"). Practicing
# against the REAL tier boundary is genuine orientation value (this is
# literally onboarding step 4, "understand tier limits") with zero real
# safety risk, since no real action ever executes either way.
PRACTICE_ACTIONS_BY_CASE_TYPE: dict[str, tuple[str, ...]] = {
    PRACTICE_CASE_TYPE_VIDEO_REVIEW: ("approve", "reject", "delete"),
    PRACTICE_CASE_TYPE_CHAT_REPORT: ("dismiss", "escalate_ban", "delete_message"),
    PRACTICE_CASE_TYPE_GUILD_REPORT: ("dismiss", "delete_content"),
}
PRACTICE_ACTION_MIN_ROLE: dict[str, AdminRole] = {
    "approve": AdminRole.MODERATOR,
    "reject": AdminRole.MODERATOR,
    "delete": AdminRole.SENIOR_MODERATOR,
    "dismiss": AdminRole.MODERATOR,
    "escalate_ban": AdminRole.MODERATOR,
    "delete_message": AdminRole.SENIOR_MODERATOR,
    "delete_content": AdminRole.SENIOR_MODERATOR,
}


@dataclass(frozen=True)
class PracticeCase:
    key: str
    case_type: str
    order: int
    # Plain English -- same established "server owns the content, the
    # frontend owns bilingual copy via its own label map" convention
    # `OnboardingStep.label` already set (see that class's own
    # docstring); `admin-moderation.html`'s own `PRACTICE_CASE_LABELS`
    # map supplies the real bilingual copy a volunteer actually reads.
    title: str
    fake_actor_name: str
    scenario: str
    content_text: str


# The real, fixed, hand-written practice-case bank -- six cases, two per
# real case type (see this module's own docstring, Scope Decisions 2/3).
# Every fake name/detail below is deliberately, unmistakably marked as
# practice content (a "(practice)" suffix on every fake person's name,
# on top of the real, separate `is_practice`-equivalent structural
# isolation this module's own docstring explains) -- belt-and-suspenders
# honesty, not the only safety mechanism.
PRACTICE_CASES: tuple[PracticeCase, ...] = (
    PracticeCase(
        key="video_clear_approve",
        case_type=PRACTICE_CASE_TYPE_VIDEO_REVIEW,
        order=1,
        title="A clear, on-topic submission",
        fake_actor_name="Omar (practice student)",
        scenario=(
            "The AI pipeline scored this 82/100 with no safety flag. The video is a straightforward "
            "5-minute walkthrough of a school science-fair project. Nothing about it looks borderline."
        ),
        content_text="\"My Science Fair Project: Growing Salt Crystals\" -- transcript excerpt: "
        "\"...so after three days the crystal had grown almost a centimeter, and here's what "
        "the structure looked like under my phone's macro lens...\"",
    ),
    PracticeCase(
        key="video_borderline_language",
        case_type=PRACTICE_CASE_TYPE_VIDEO_REVIEW,
        order=2,
        title="Borderline language in an otherwise fine video",
        fake_actor_name="Yara (practice student)",
        scenario=(
            "The AI pipeline flagged this one (safety_flag=true) for a single frustrated exclamation "
            "partway through an otherwise genuine, on-topic coding tutorial. This is exactly the kind "
            "of case real Moderators are asked to use real judgment on -- there is no single scripted "
            "right answer here; the point of this practice case is the WORKFLOW, not a graded outcome."
        ),
        content_text="\"Building My First To-Do App\" -- transcript excerpt: \"...and then I spent "
        "two hours debugging this one line, which was -- honestly, extremely frustrating -- before "
        "I realized I'd misspelled the variable name...\"",
    ),
    PracticeCase(
        key="chat_obvious_spam",
        case_type=PRACTICE_CASE_TYPE_CHAT_REPORT,
        order=1,
        title="An obvious spam/advertising message",
        fake_actor_name="Karim (practice reporter)",
        scenario="One real-shaped report, reason 'spam', on a message posted three times in a row "
        "in a study-group chat room.",
        content_text="\"WIN FREE PRIZES NOW visit definitely-not-a-scam.example and claim yours "
        "before it's gone!!!\"",
    ),
    PracticeCase(
        key="chat_harassment_report",
        case_type=PRACTICE_CASE_TYPE_CHAT_REPORT,
        order=2,
        title="A harassment report between two students",
        fake_actor_name="Nourhan (practice reporter)",
        scenario="One real-shaped report, reason 'harassment', describing a repeated pattern of "
        "unwelcome comments directed at the reporter in a course chat room. A second, real Moderator "
        "would normally want the full message thread's context before deciding -- this practice case "
        "shows only the reported message itself, on purpose, to rehearse recognizing when a case needs "
        "more context before deciding, not just the mechanics of clicking an action.",
        content_text="\"why do you even bother showing up to these calls, nobody wants you here\"",
    ),
    PracticeCase(
        key="guild_post_false_report",
        case_type=PRACTICE_CASE_TYPE_GUILD_REPORT,
        order=1,
        title="A guild post that turns out to be fine",
        fake_actor_name="Hassan (practice author)",
        scenario="One real-shaped report, reason 'other', on a guild post that -- once actually read "
        "-- is genuinely on-topic study-group coordination with nothing wrong with it. A real reminder "
        "that not every report describes a real violation.",
        content_text="\"Anyone free to review my practice exam answers before Thursday? I can share "
        "my notes in exchange :)\"",
    ),
    PracticeCase(
        key="guild_comment_real_violation",
        case_type=PRACTICE_CASE_TYPE_GUILD_REPORT,
        order=2,
        title="A guild comment with a real violation",
        fake_actor_name="Mostafa (practice author)",
        scenario="One real-shaped report, reason 'inappropriate_content', on a comment reply that "
        "genuinely violates the community's own posted rules.",
        content_text="\"lol who cares, just cheat on it like everyone else does\"",
    ),
)
PRACTICE_CASE_KEYS: frozenset[str] = frozenset(case.key for case in PRACTICE_CASES)
PRACTICE_CASES_BY_KEY: dict[str, PracticeCase] = {case.key: case for case in PRACTICE_CASES}


class PracticeCaseAttempt(Base):
    """One row per (volunteer, practice case) -- records ONLY the real
    choice a volunteer made rehearsing a practice case (`action_taken`)
    and when. Re-attempting the SAME case UPSERTS this row (unlike
    `OnboardingChecklistProgress`'s own insert-once-if-absent shape --
    see that model's own docstring): there is no real "correctness" to
    preserve a first answer against here (Scope Decision 1 above), so
    the real, honest state worth keeping is the volunteer's MOST RECENT
    choice, letting them genuinely re-rehearse a case as many times as
    they want.

    STRUCTURALLY ISOLATED from every real table on purpose (see this
    module's own docstring, STEP 3.1) -- no FK to `student_videos`/
    `chat_message_reports`/`guild_content_reports`, and nothing in
    `app/services/practice_case_service.py` ever reads or writes any of
    those tables, `moderation_log_entries`, `notifications`, or the
    chat-ban Redis store.
    """

    __tablename__ = "practice_case_attempts"
    __table_args__ = (
        UniqueConstraint("user_id", "case_key", name="uq_practice_attempt_user_case"),
        Index("ix_practice_attempts_user_id", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )

    # Validated against `PRACTICE_CASE_KEYS` at the API layer -- same
    # "plain String, not a DB enum, for a small team-editable list"
    # reasoning `OnboardingChecklistProgress.step_key`'s own docstring
    # already gives.
    case_key: Mapped[str] = mapped_column(String(50), nullable=False)
    action_taken: Mapped[str] = mapped_column(String(30), nullable=False)
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)

    acted_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return (
            f"<PracticeCaseAttempt user_id={self.user_id} case_key={self.case_key!r} "
            f"action_taken={self.action_taken!r}>"
        )
