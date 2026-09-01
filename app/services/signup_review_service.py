"""Review of Suspicious New Signups -- service layer for
`GET /admin/signups/review`.

Investigation summary (this task's own required Step 1, kept here as
the real, durable record rather than only in a chat reply):

1. This backend has NO local registration endpoint at all -- confirmed
   again by re-reading `app/api/deps.py::_resolve_or_provision_user` in
   full (already known from the Supabase-integration step, but this
   task's own Step 1.1 asks to confirm precisely, not just recall it).
   Supabase is the sole auth authority; the FIRST time a request arrives
   bearing a valid Supabase JWT for a `sub` UUID this backend hasn't
   seen, a `User` row is auto-provisioned on the spot with exactly TWO
   real fields populated: `id` (the token's `sub`) and `email` (the
   token's `email` claim, itself optional). Every other column --
   `full_name`, `phone`, `degree_level`, `major`, `gpa`,
   `graduation_year`, `skills`, `bio`, `academic_level`, `gender`, `age`,
   `target_country` -- starts `NULL` and is only ever filled in later,
   voluntarily, via `PATCH /users/me`. There is no local signup FORM to
   have collected anything richer (`signup.html`'s own account-creation
   step is a static mock, confirmed in an earlier phase) -- Supabase
   itself owns whatever it collects (password, phone/email verification
   state, IP, device fingerprint, etc.), and none of that is ever
   forwarded to or visible from this backend. This is the single most
   important real constraint this feature's whole design has to honor:
   there is no rich signup payload anywhere in this system to mine for
   "suspicious" signals.

2. Step 1.2's own question -- does completing the real profile fields
   (academic_level/gender/age, from the Eligibility Filtering phase)
   correlate with account legitimacy -- is NOT knowable from this
   system's real data, and no attempt is made here to guess at it. There
   is no ground truth anywhere in this codebase distinguishing a "real"
   account from a "fake" one (no verification step, no manual
   confirmed-legitimate flag, nothing) to correlate profile-completion
   against in the first place -- inventing a "incomplete profile ==
   suspicious" rule with nothing behind it would be exactly the kind of
   fabricated, unexplainable signal this project has consistently
   refused to ship (Career Compass's real deterministic RIASEC scoring
   over an LLM guess; High-Risk-Content View's flat refusal to invent a
   composite score). Stated plainly, disclosed here, and NOT built:
   profile-completion is not used as a signal anywhere in this module.

3. `app/api/dependencies/rate_limiter.py` (read in full) is entirely
   about capping `StudentVideo` UPLOADS per user per rolling 24h window
   -- it tracks nothing about account CREATION at all, and there is no
   separate signup-velocity/rate-limiting infrastructure anywhere else
   in this codebase (confirmed via a full grep for
   "signup"/"registration"/"velocity"/"rate_limit" across `app/`). There
   is therefore no existing infrastructure to reuse for a velocity
   signal -- but `User.created_at` (a real, always-populated,
   server-set timestamp -- see `TimestampMixin`) is itself real data
   this module computes a genuine velocity signal FROM directly, with no
   new tracking infrastructure needed.

Design (Step 2), stated plainly per this task's own explicit
"acceptable, and preferable, to report a narrower real scope" framing:
this feature's real, honest scope is a sortable, real "recently joined"
list, each account annotated with up to three independently real,
plain-English-explainable facts -- never an opaque composite score:

- **Velocity**: how many OTHER accounts were created within
  `SIGNUP_VELOCITY_WINDOW_MINUTES` of this one. A real, direct
  computation over `User.created_at` -- many accounts appearing in a
  tight window is a genuine, classic signal of scripted/bulk account
  creation, and needs no new tracking to compute.
- **Already blacklist-triggered**: this account has a real
  `ModerationLogEntry` row with `action=BLACKLIST_TRIGGERED` and
  `target_id=<this user>` created since this account itself was
  created (see the General Content Blacklist / Pattern Auto-Trigger
  phase) -- a brand-new account whose very first real actions on the
  platform already tripped the content filter is a genuine, real,
  system-verified signal, not an inference.
- **Already moderated**: this account has a real punitive
  `ModerationLogEntry` (the SAME closed set `repeat_offender_service`
  already established and tests) resolved against them since this
  account was created -- reused via the new
  `repeat_offender_service.get_offense_events_since` (a small, honest
  extension of that module, not a second copy of its offender-
  resolution logic).

Deliberately NOT built: any profile-completion signal (see Step 1.2
above), any IP/device/fingerprint signal (never available to this
backend at all -- Supabase-only), and any single unified "risk score"
combining the three real signals above into one number (same
explainability stance as High-Risk-Content View -- each signal is
surfaced on its own real, plain-English basis).
"""

import dataclasses
import datetime as dt
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.enums import ModerationAction
from app.models.moderation_log import ModerationLogEntry
from app.models.user import User
from app.services import repeat_offender_service

# Named constants, per this project's own established "no magic
# numbers" discipline.

# How far back "recently joined" looks. Deliberately shorter than
# `repeat_offender_service.REPEAT_OFFENDER_LOOKBACK_DAYS` (30) -- that
# window answers "does this person have an ongoing pattern," a
# genuinely different question from this feature's own "who joined
# recently enough that a first-days review is still useful." 14 days
# (two weeks) is long enough to catch a signup a moderator didn't get to
# review immediately, short enough that this stays a real "new signups"
# view rather than slowly turning into a second copy of the full user
# directory.
RECENT_SIGNUP_LOOKBACK_DAYS = 14

# How tight a window counts as "clustered" for the velocity signal. 15
# minutes -- tight enough that genuine coincidental overlap (two real
# students happening to sign up in the same hour during a class
# orientation session, say) is unlikely to trigger it, loose enough to
# catch a real scripted burst without needing exact-same-second
# precision.
SIGNUP_VELOCITY_WINDOW_MINUTES = 15


@dataclasses.dataclass
class RecentSignupCandidate:
    user_id: uuid.UUID
    full_name: str | None
    email: str | None
    created_at: dt.datetime
    cohort_size: int
    blacklist_trigger_count: int
    moderation_action_count: int


async def get_recent_signups(
    db: AsyncSession,
    *,
    window_days: int = RECENT_SIGNUP_LOOKBACK_DAYS,
    velocity_window_minutes: int = SIGNUP_VELOCITY_WINDOW_MINUTES,
    limit: int = 50,
    offset: int = 0,
) -> tuple[list[RecentSignupCandidate], int]:
    """Real recently-created accounts within `window_days`, each
    annotated with the three real signals this module's own docstring
    describes. Staff accounts (`admin_role IS NOT NULL`) are excluded --
    those are internally created/promoted, not organic public signups,
    and including them would be a real false-positive risk (an Owner's
    own bootstrap account, or a newly-promoted Moderator, would
    otherwise show up in a "suspicious new signups" list for no honest
    reason). Sorted with any real signal present first (most
    velocity-clustered, then most blacklist triggers, then most
    moderation actions), then by `created_at` descending -- so a
    moderator opening this view sees the accounts most worth a look
    first, without the ranking pretending to be a single opaque score.
    """
    cutoff = dt.datetime.now(dt.timezone.utc) - dt.timedelta(days=window_days)

    # Velocity needs every account created since `cutoff`, not just the
    # current page -- an account near a page boundary must still see
    # every real neighbor within its own window, including ones that
    # would sort onto a different page. Fetched once, sorted, and
    # cross-referenced in Python via a plain sliding window -- no new
    # detection logic, just real arithmetic over real timestamps.
    all_recent_result = await db.execute(
        select(User.id, User.full_name, User.email, User.created_at)
        .where(User.created_at >= cutoff, User.admin_role.is_(None))
        .order_by(User.created_at.asc())
    )
    all_recent = all_recent_result.all()
    total = len(all_recent)

    velocity_window = dt.timedelta(minutes=velocity_window_minutes)
    timestamps = [row.created_at for row in all_recent]

    def cohort_size(idx: int) -> int:
        """Count of OTHER accounts (excludes the account itself) whose
        `created_at` falls within `velocity_window` on either side of
        `all_recent[idx]`'s own `created_at`. `all_recent` is sorted
        ascending by `created_at`, so this is a plain two-pointer scan,
        not a per-row query."""
        ts = timestamps[idx]
        lo = idx
        while lo > 0 and ts - timestamps[lo - 1] <= velocity_window:
            lo -= 1
        hi = idx
        while hi < len(timestamps) - 1 and timestamps[hi + 1] - ts <= velocity_window:
            hi += 1
        return (hi - lo + 1) - 1  # -1 excludes the account itself

    # Real punitive-moderation and blacklist-trigger events since
    # `cutoff` -- fetched once each, cross-referenced in Python, same
    # "no N+1" posture as the velocity computation above.
    offense_events = await repeat_offender_service.get_offense_events_since(db, cutoff)
    moderation_counts: dict[uuid.UUID, int] = {}
    for offender_id, _entry_created_at in offense_events:
        moderation_counts[offender_id] = moderation_counts.get(offender_id, 0) + 1

    blacklist_result = await db.execute(
        select(ModerationLogEntry.target_id)
        .where(
            ModerationLogEntry.action == ModerationAction.BLACKLIST_TRIGGERED,
            ModerationLogEntry.created_at >= cutoff,
        )
    )
    blacklist_counts: dict[uuid.UUID, int] = {}
    for (target_id,) in blacklist_result.all():
        blacklist_counts[target_id] = blacklist_counts.get(target_id, 0) + 1

    candidates = [
        RecentSignupCandidate(
            user_id=row.id,
            full_name=row.full_name,
            email=row.email,
            created_at=row.created_at,
            cohort_size=cohort_size(idx),
            blacklist_trigger_count=blacklist_counts.get(row.id, 0),
            moderation_action_count=moderation_counts.get(row.id, 0),
        )
        for idx, row in enumerate(all_recent)
    ]

    candidates.sort(
        key=lambda c: (
            c.cohort_size,
            c.blacklist_trigger_count,
            c.moderation_action_count,
            c.created_at,
        ),
        reverse=True,
    )

    return candidates[offset : offset + limit], total
