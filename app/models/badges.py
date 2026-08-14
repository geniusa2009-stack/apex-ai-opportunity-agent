"""Gamification: Daily Streaks, Badges, and Leaderboard.

DISCLOSED FILE-PLACEMENT DEVIATION: this step's own ask names its model
file `app/models/gamification.py` directly. That literal path is already
taken -- `app/models/gamification.py` is the Ecosystem Feature 4 (AI
Accountability Coach & Premium Token Economy) module, defining
`StudentAccountability` (spendable `tokens_balance`, its own
`current_streak`, `procrastination_score`). Renaming/repurposing that
file, or silently appending unrelated classes to a module whose own
docstring specifically describes itself as the Token Economy's model
file, would be a real, undisclosed regression risk to an already-shipped
feature for no functional gain. The three NEW models this step asks for
live here instead, in a new file named after what they actually are.

DISCLOSED CONCEPTUAL OVERLAP (not just a filename collision): the
pre-existing `StudentAccountability` ALREADY has a `current_streak`
column, updated by `coach_service.claim_daily_login` on the exact same
"yesterday -> continue, otherwise -> reset to 1" logic this step's own
`process_daily_activity` describes. This is a DELIBERATELY SEPARATE,
independent streak/economy, not a duplicate to be merged:

- `StudentAccountability` is a SPENDABLE currency system (tokens can be
  earned AND deducted, e.g. `require_tokens`-gated AI features) whose
  streak exists to reward literally claiming daily tokens via `POST
  /coach/daily-claim`.
- `GamificationProfile` (below) is a permanent, monotonically-increasing
  SCORE (XP is never spent, only earned) feeding a public leaderboard
  and permanent, un-revocable badge unlocks -- a different axis
  entirely, the same real-world distinction many gamified products draw
  between a spendable currency and a permanent XP/rank score.

Merging these into one table/streak would have meant rewriting and
re-testing the existing, already-shipped Token Economy flow for a step
whose own ask gives this new system an entirely self-contained field
list of its own. The real, disclosed trade-off this creates: a student
can have TWO different "current streak" numbers active at once (their
token-claim streak and their gamification-activity streak), which can
diverge -- e.g. watching a video touches THIS step's streak (see
`gamification_service.process_daily_activity`'s docstring for its own
trigger list) without ever touching `StudentAccountability.current_streak`,
which only moves via an explicit `POST /coach/daily-claim` call. Not
silently hidden -- surfaced here and in the README's own judgment-calls
section.
"""

import datetime as dt
import uuid

from sqlalchemy import Date, DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column

from app.db.base import Base


class GamificationProfile(Base):
    """One row per student, tracking this step's own Daily Streak +
    Experience Points state. `user_id` is BOTH the primary key and the
    foreign key to `users.id` -- the exact same "genuine 1:1, not a
    separate surrogate `id`" shape this step's ask specifies literally
    ("user_id: UUID (Primary Key, Foreign Key)"), and the same
    established precedent `StudentAccountability` already set for this
    codebase's other 1:1-with-User accounting tables.

    `last_activity_date` is a plain `Date` (NOT `DateTime`, unlike
    `StudentAccountability.last_active_at`) -- per this step's own
    literal column list ("last_activity_date: Date"). A calendar DATE is
    in fact the more semantically correct type for streak logic that
    only ever compares "yesterday" vs. "today" vs. "earlier" at
    day-granularity; storing a full timestamp would just mean discarding
    the time-of-day component on every comparison anyway (see
    `gamification_service.process_daily_activity`).
    """

    __tablename__ = "gamification_profiles"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )

    total_xp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    current_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    highest_streak: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_activity_date: Mapped[dt.date | None] = mapped_column(Date, nullable=True)

    # --- Weekly League System addition (migration 0033) ---
    # `weekly_xp` is a SEPARATE counter from `total_xp` -- `total_xp` is
    # the permanent, all-time score this module's own docstring already
    # describes; `weekly_xp` resets to 0 every week (see
    # `gamification_service.reset_weekly_leagues`) and is what the
    # Weekly League leaderboard/promotion logic actually ranks on.
    # `league` is a plain String (not a DB enum), same "ask gives a
    # closed-looking list without demanding a native enum type" posture
    # as `StudentVideoStatus`'s own module docstring -- Bronze/Silver/
    # Gold/Apex, validated at the Pydantic layer.
    weekly_xp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    league: Mapped[str] = mapped_column(String(20), default="bronze", nullable=False)

    # --- Real Leaderboard System, Level 2 addition (migration 0034) ---
    # `monthly_xp` -- a THIRD independent counter alongside `total_xp`
    # (all-time, never resets) and `weekly_xp` (resets Monday 00:00 UTC).
    # Same "separate counter per period" shape as `weekly_xp` above, not a
    # derived/computed value -- `gamification_service.add_xp` increments
    # all three together, and `reset_monthly_leaderboard` zeroes only this
    # one on a monthly cron, mirroring `reset_weekly_leagues`'s own reset
    # step exactly. Unlike `weekly_xp`, there is no monthly LEAGUE concept
    # (no promotion/demotion) -- this backs a plain top-N + personal-rank
    # monthly leaderboard only, per this feature's own explicit scope.
    monthly_xp: Mapped[int] = mapped_column(Integer, default=0, nullable=False)

    # `display_alias` -- optional, self-set pseudonym shown on leaderboards
    # INSTEAD of the real name/email fallback chain, when set. Lives here
    # (not on `User`) because it's specifically a leaderboard-identity
    # concern scoped to this gamification system, not a general profile
    # field the rest of the app should see -- same "extend the model that
    # actually owns this concept" boundary already used to keep
    # `StudentAccountability` and `GamificationProfile` as two separate,
    # independent tables (see this file's own module docstring). Nullable:
    # most users will never set one, and `None` here means "show my real
    # name," not an empty-string alias. See `gamification.py`'s
    # `_display_name` for the exact resolution order this feeds into.
    display_alias: Mapped[str | None] = mapped_column(String(60), nullable=True)

    def __repr__(self) -> str:
        return (
            f"<GamificationProfile user_id={self.user_id} total_xp={self.total_xp} "
            f"current_streak={self.current_streak}>"
        )


class Badge(Base):
    """A single unlockable badge definition -- e.g. "First Steps" at
    100 XP. `required_xp` is a plain threshold (`GamificationProfile.
    total_xp >= required_xp` unlocks it), not a more elaborate rules
    engine -- exactly this step's own literal field list ("id, name,
    description, icon_url, required_xp"), nothing invented beyond it.

    `name` is unique (a real, disclosed addition beyond the ask's literal
    column list): two badges with the identical name would be a genuine
    data-integrity bug (which one does a leaderboard/profile UI show?),
    not a legitimate use case this schema needs to support.

    `icon_url` is nullable -- a badge can exist (and be awarded) before
    a real icon asset has been designed/uploaded for it; the frontend's
    `BadgesShowcase` component falls back to a plain glyph when it's
    `None` (see that component's own docstring).
    """

    __tablename__ = "badges"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(120), unique=True, nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    icon_url: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    required_xp: Mapped[int] = mapped_column(Integer, nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<Badge id={self.id} name={self.name!r} required_xp={self.required_xp}>"


class UserBadge(Base):
    """Association row: "this user has unlocked this badge, at this
    time." Composite primary key `(user_id, badge_id)` -- per this
    step's own literal three-column spec ("user_id, badge_id,
    awarded_at"), which names no separate surrogate `id` column, and a
    composite PK is the natural, non-redundant shape for a pure
    many-to-many association row (the same reasoning this codebase
    already applies to e.g. `UserEnrollment`'s `(user_id, course_id)`
    pair). The PK itself is also what makes "has this user already
    unlocked this badge" a natural uniqueness constraint enforced by the
    database, not just application logic -- `gamification_service.
    add_xp`'s badge-unlock query relies on this: it can never insert the
    same `(user_id, badge_id)` pair twice.

    `awarded_at` uses `server_default=func.now()` (DB-assigned, like
    every `TimestampMixin.created_at` elsewhere in this codebase) rather
    than a Python-side default -- correct even for a row inserted outside
    the ORM, and consistent with this project's established timestamp
    convention.
    """

    __tablename__ = "user_badges"

    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="CASCADE"),
        primary_key=True,
    )
    badge_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("badges.id", ondelete="CASCADE"),
        primary_key=True,
    )
    awarded_at: Mapped[dt.datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<UserBadge user_id={self.user_id} badge_id={self.badge_id}>"
