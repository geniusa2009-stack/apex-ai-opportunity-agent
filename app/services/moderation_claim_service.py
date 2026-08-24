"""Claim System (Prevent Duplicate Work on the Same Case) -- service layer.

See `app/api/v1/endpoints/admin_claims.py`'s own module docstring for the
full Step 1 "what is a case" investigation. Short version: the review
queue's only real item kind today is a `StudentVideo` row (id, status,
etc. -- `admin_moderation.py::get_review_queue`'s own query), so a claim
is `claimed_by`/`claimed_at` columns added directly to that model, not a
new polymorphic table. Every function here is written against a plain
"has `claimed_by`/`claimed_at` attributes" duck-typed object rather than
importing `StudentVideo` by name, so a FUTURE claimable item type (a
different queue this codebase doesn't have yet) only needs those same
two columns to reuse this whole module unchanged -- see
`admin_claims.py`'s own `_CLAIMABLE_MODELS` dispatch table for where
that extension point actually lives.

DISCLOSED, DELIBERATE EXPIRY DESIGN: lazy, read-time expiry, NOT a
scheduled Celery job. The chat ban TTL (`app/services/chat_security.py`)
is this codebase's own precedent for "a time-based state that lifts
itself with no cleanup job" -- but that precedent works by storing the
ban IN Redis and using Redis's own native `EX`/`TTL` mechanism, which
Postgres has no equivalent of. A claim lives on a real Postgres row
(see above for why), so there is no server-side auto-expiring primitive
to lean on here the way `chat_security.ban_user` can. The two remaining
options were: (a) a real Celery Beat job that periodically scans for and
clears stale claims, or (b) compute "is this claim still active" at
every real read/write site from `claimed_at` + `CLAIM_TIMEOUT_MINUTES`,
with no separate cleanup step at all. (b) is chosen -- it needs no new
scheduled task, can never drift out of sync with "wall-clock time right
now" the way a periodic job polling on an interval could (a claim
becomes stale exactly when read, not up to one poll-interval late), and
matches this module's own small blast radius (the ONLY things that ever
read `claimed_by`/`claimed_at` are the two claim endpoints and the
review-queue listing, all three of which already go through
`get_effective_claim` below).
"""

import datetime as dt
import uuid
from typing import Protocol

# Real, named constant -- not a magic number scattered across the claim
# endpoints and the review-queue listing. 30 minutes: long enough that a
# volunteer reading a flagged video's transcript/watching its preview
# and writing up notes doesn't get silently un-claimed out from under
# them mid-review, short enough that a claim abandoned mid-session (the
# literal "opened it, got distracted, never resolved it" case this
# step's own ask names) doesn't block the item for the rest of the day.
CLAIM_TIMEOUT_MINUTES = 30


class _Claimable(Protocol):
    """Structural type for `get_effective_claim`/`apply_claim`/
    `apply_release` below -- currently only ever `StudentVideo`, but
    written as a Protocol (not a concrete import) so this whole module
    stays reusable unchanged for a future second claimable item type."""

    claimed_by: uuid.UUID | None
    claimed_at: dt.datetime | None


def _now() -> dt.datetime:
    """One seam for "the current time," so tests can reason about expiry
    without real wall-clock sleeps -- see `tests/test_moderation_claims.py`'s
    own use of a fixed `claimed_at` in the past instead of monkeypatching
    this function directly (simpler: control the STORED timestamp, not
    the clock)."""
    return dt.datetime.now(dt.timezone.utc)


def get_effective_claim(item: _Claimable) -> tuple[uuid.UUID | None, dt.datetime | None]:
    """The one real place "is this item currently claimed, and by whom"
    gets decided -- every other function/endpoint in this feature calls
    THIS rather than reading `item.claimed_by`/`item.claimed_at`
    directly, so the expiry rule can never be applied inconsistently in
    one call site and forgotten in another.

    Returns `(None, None)` for "effectively unclaimed" -- covers THREE
    real cases identically: never claimed at all (`claimed_by IS NULL`),
    a claim whose `CLAIM_TIMEOUT_MINUTES` window has lapsed, and a claim
    whose claimant account was since deleted (`ON DELETE SET NULL` --
    see the column's own docstring). Returns the real, live
    `(claimed_by, claimed_at)` pair otherwise.
    """
    if item.claimed_by is None or item.claimed_at is None:
        return None, None

    claimed_at = item.claimed_at
    if claimed_at.tzinfo is None:
        # Defensive: a naive datetime read back from a DB driver that
        # doesn't preserve tzinfo (shouldn't happen against the real
        # `DateTime(timezone=True)` column, but SQLite's own test
        # fixtures store this as plain TEXT -- see that fixture's own
        # comment) is treated as UTC, matching `_now()`'s own tzinfo.
        claimed_at = claimed_at.replace(tzinfo=dt.timezone.utc)

    age = _now() - claimed_at
    if age > dt.timedelta(minutes=CLAIM_TIMEOUT_MINUTES):
        return None, None

    return item.claimed_by, item.claimed_at


class ClaimConflictError(Exception):
    """Raised by `apply_claim` when the item is actively claimed by
    someone OTHER than the caller -- the router translates this into a
    real 409."""

    def __init__(self, claimed_by: uuid.UUID, claimed_at: dt.datetime) -> None:
        self.claimed_by = claimed_by
        self.claimed_at = claimed_at
        super().__init__(f"Item is already claimed by {claimed_by} since {claimed_at.isoformat()}")


def apply_claim(item: _Claimable, *, caller_id: uuid.UUID) -> None:
    """Mutates `item.claimed_by`/`item.claimed_at` in place (caller adds
    it to their own session/commits, same "service returns a mutated
    object, doesn't manage its own transaction" posture as
    `moderation_log_service.log_action`).

    Three real outcomes, per this step's own literal spec:
    - Unclaimed (or expired, or claimant-account-deleted -- all three
      collapse to "effectively unclaimed" via `get_effective_claim`
      above): claims it fresh.
    - Already claimed by `caller_id`: idempotent success, and
      DELIBERATELY refreshes `claimed_at` to `_now()` rather than
      leaving the original timestamp untouched -- a second claim call
      from the SAME volunteer is a real signal they're still actively
      working the item (a "heartbeat"), so re-claiming is the one
      natural way this codebase's own lazy-expiry design lets an
      in-progress review avoid silently expiring out from under someone
      still on it, with no separate "extend my claim" endpoint needed.
    - Actively claimed by someone else: raises `ClaimConflictError`
      (never mutates `item`).
    """
    effective_claimed_by, effective_claimed_at = get_effective_claim(item)

    if effective_claimed_by is not None and effective_claimed_by != caller_id:
        assert effective_claimed_at is not None  # get_effective_claim's own invariant
        raise ClaimConflictError(effective_claimed_by, effective_claimed_at)

    item.claimed_by = caller_id
    item.claimed_at = _now()


def apply_release(item: _Claimable) -> None:
    """Clears the claim unconditionally -- callers (the DELETE endpoint)
    are responsible for the real authorization decision (claimant or
    Owner, see that route's own docstring) BEFORE calling this; this
    function itself has no caller-identity concept, mirroring
    `apply_claim`'s own "service does the mutation, router decides who's
    allowed to trigger it" split.

    Deliberately unconditional (no "was it even claimed" guard) --
    releasing an already-unclaimed/already-expired item is a harmless
    no-op write, and treating it as an error would make the release
    endpoint's own idempotent-success posture (see its docstring)
    impossible to implement cleanly.
    """
    item.claimed_by = None
    item.claimed_at = None
