"""Schemas for GET /wallet/dashboard and GET /wallet/opportunities.

Deliberately does NOT introduce a new `UserWallet`/`Streak` model or a
stored `current_tier` column. The real token-economy state already lives
in `StudentAccountability` (app/models/gamification.py: tokens_balance,
total_earned_tokens, current_streak) and `GamificationProfile`
(app/models/badges.py: total_xp, current_streak, highest_streak) --
both shipped by prior steps and covered by tests/test_gamification.py /
the coach_service tests. `/wallet/dashboard` is a read-only composition
over those two existing rows plus a derived `current_tier`, not a new
source of truth -- adding a third, parallel "wallet" table/column here
would just create a second place tier/balance could drift out of sync
with the accountability record `POST /coach/daily-claim` and every
`require_tokens`-gated feature already write to.

`current_tier` is computed in `coach_service.compute_tier` from
`total_earned_tokens` (lifetime-earned, not spendable balance -- so
spending tokens on a course/feature never demotes a user's tier) and
returned here as a plain string; it is NOT persisted, so there is
nothing to migrate.
"""

from uuid import UUID

from pydantic import BaseModel


class WalletDashboardOut(BaseModel):
    user_id: UUID
    tokens_balance: int
    lifetime_earned: int
    current_tier: str
    tokens_to_next_tier: int | None
    next_tier: str | None
    current_streak_days: int
    longest_streak: int
    coach_insight: str


class OpportunityOut(BaseModel):
    """One real, currently-wired way to spend tokens on the platform.
    Sourced directly from the live `require_tokens(...)` / token-cost
    constants already in the codebase (essay_humanizer.TOKEN_COST,
    multi_agent_interview.TOKEN_COST) plus course token-gating
    (Course.token_unlock_cost, which varies per course rather than
    being one fixed number, so it's surfaced as a range/"varies" entry
    instead of an invented flat price). No mentor-session token price
    exists anywhere in the backend (mentorship booking is not
    token-gated) -- so it is intentionally NOT listed here rather than
    invented."""

    action: str
    cost: int | None
    cost_label: str
    feature_key: str
