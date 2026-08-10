import datetime as dt
from uuid import UUID

from pydantic import BaseModel, ConfigDict


class BadgeOut(BaseModel):
    """One unlocked badge, as returned inside `GamificationStatsOut.badges`
    and `DailyLoginOut.newly_unlocked_badges`."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    name: str
    description: str
    icon_url: str | None
    required_xp: int


class GamificationStatsOut(BaseModel):
    """`GET /gamification/my-stats` response -- this step's own literal
    "current user's XP, Streak, and unlocked Badges" requirement, read
    directly off `GamificationProfile` plus a separately-fetched badges
    list (a `GamificationProfile` -> `Badge` relationship isn't declared
    on the ORM model itself -- see `app/models/badges.py`'s own
    docstring on why `UserBadge` stays a plain association row rather
    than a mapped `relationship()` -- so the router builds this schema
    from two separate service calls, not one `model_validate`)."""

    user_id: UUID
    total_xp: int
    current_streak: int
    highest_streak: int
    last_activity_date: dt.date | None
    badges: list[BadgeOut]


class LeaderboardEntryOut(BaseModel):
    """One row of `GET /gamification/leaderboard`'s top-10 response.

    `rank` is 1-based (not a raw array index) -- directly usable by a
    frontend rendering "#1", "#2", etc. without an off-by-one
    adjustment. `display_name` falls back to `email` when `full_name` is
    unset (a real, reachable case -- see `app/models/user.py`'s own
    disclosed nullable-`full_name`/`email` posture), and to a generic
    "Anonymous Student" label if BOTH are unset (Supabase's `email`
    claim is itself optional, per `app/api/deps.py`'s auto-provisioning
    docstring) -- a public leaderboard should never render a raw `None`
    or an empty name.
    """

    rank: int
    user_id: UUID
    display_name: str
    total_xp: int
    current_streak: int


class DailyLoginOut(BaseModel):
    """`POST /gamification/daily-login` response -- this step's own
    concrete "logs in" trigger for `process_daily_activity`, mirroring
    `schemas/coach.py`'s `DailyClaimOut` shape for the pre-existing,
    separate Token Economy daily-claim endpoint (see `app/models/
    badges.py`'s module docstring for why these are two independent
    systems). `newly_unlocked_badges` surfaces anything `add_xp` just
    unlocked so a frontend can show an immediate "Badge Unlocked!"
    moment right on login, rather than the student only discovering it
    later on their stats page.
    """

    xp_awarded: int
    total_xp: int
    current_streak: int
    highest_streak: int
    newly_unlocked_badges: list[BadgeOut]


class WeeklyLeagueEntryOut(BaseModel):
    """One row of `GET /gamification/leagues/leaderboard` -- top members
    of the caller's OWN league, ranked by `weekly_xp`."""

    rank: int
    user_id: UUID
    display_name: str
    weekly_xp: int


class WeeklyLeagueOut(BaseModel):
    """`GET /gamification/leagues/leaderboard` response -- podium/rank
    list plus the caller's own standing, per this feature's own "podium
    for top 3, user's current rank, progress bar to next league" UI
    spec."""

    league: str
    weekly_xp: int
    rank: int
    league_size: int
    xp_to_next_rank: int
    top_members: list[WeeklyLeagueEntryOut]
