"""English-Only Gamified Real-time Chat -- Redis-backed ban service +
the Arabic-detection filter.

This step's own ask names exactly two functions: `ban_user(user_id,
duration_seconds=1800)` and `is_user_banned(user_id)`. Both are
implemented here verbatim. `contains_arabic` (the ask's own regex,
`[\\u0600-\\u06FF]`) also lives here rather than inline in the WebSocket
router -- it's a pure, independently-testable predicate with no I/O, and
keeping it alongside the ban service it triggers (rather than in
`app/api/v1/endpoints/chat.py`) means `tests/test_chat.py` can exercise
the exact filter logic without needing a running WebSocket at all.

DISCLOSED NECESSARY ADDITION beyond the ask's own two named functions:
`get_ban_remaining_seconds(user_id)`. The ask's own UI requirement
("show a prominent red countdown timer") only has a value to count down
FROM at the moment a `banned` WebSocket event actually arrives -- but a
banned student who refreshes the page (or opens the chat in a new tab)
mid-ban has no WebSocket connection to receive that event from at all
(the whole point of the ban is that their connection gets REJECTED). Without
a way to ask "am I currently banned, and for how much longer?" outside of
that one WebSocket event, the countdown timer would silently reset to
nothing on every page reload during an active ban. Redis's own `TTL`
command is used here (not the app tracking a separate expiry timestamp)
so this can never drift from the actual ban's real remaining lifetime --
see `app/api/v1/endpoints/chat.py`'s `GET /chat/ban-status` REST route,
which exposes this to the frontend on page load.

REDIS CLIENT, DISCLOSED: this is the first feature in this codebase to
use Redis directly for anything other than Celery's own broker/result-
backend plumbing (`app/core/celery_app.py`) -- no prior app-level Redis
client convention existed to follow. A single module-level `redis.asyncio.
Redis` client is created lazily (`_get_redis_client()`), reusing the
EXISTING `settings.redis_url` setting (the same Redis instance Celery
already depends on being reachable -- no new infrastructure, no new
setting). `redis.asyncio` (not the sync `redis` client) matches this
entire codebase's async-everywhere posture (async SQLAlchemy, async
FastAPI routes, async WebSocket handlers).
"""

import re
import uuid

import redis.asyncio as aioredis

from app.core.config import settings

# The ask's own literal regex, `[؀-ۿ]` (the Unicode "Arabic"
# block), compiled once at import time rather than on every call. Written
# below using the literal characters at each end of that range (U+0600,
# U+06FF) rather than the `\uXXXX` escape spelling -- functionally
# identical (confirmed: `re`'s own engine recognizes `\uXXXX` escapes
# inside a pattern regardless of raw-string-ness, and compiles to the
# exact same character range either way), chosen only because a bare
# character range reads unambiguously in any editor without relying on
# escape-sequence parsing rules.
_ARABIC_CHAR_PATTERN = re.compile("[؀-ۿ]")

_BAN_KEY_PREFIX = "chat:ban:"

# Manual Timed Bans addition -- real, named bounds for ANY chat ban
# duration, reused by every real caller (the escalate-ban schema,
# `AdminChatReportEscalateBanRequest.duration_seconds`, already used
# these exact numbers as raw Pydantic `ge`/`le` literals before this
# task; the new direct manual-ban schema, `AdminManualBanRequest`,
# reuses them too -- ONE real source of truth, not two schemas that
# could quietly drift apart). Enforced HERE too, not just at the two
# schemas' own Pydantic boundary -- Step 1.2's own confirmed finding:
# before this task, `ban_user` itself accepted an arbitrary
# `duration_seconds` with no floor or ceiling at all; a future caller
# that built its own request path (or a bug in a schema) could still
# have hit Redis with a nonsensical TTL. This is real defense-in-depth,
# not redundant with the schema-level `ge`/`le` -- the schema gives a
# clean `422` at the API boundary before any DB/Redis work happens; this
# is the last real line of defense for anything that calls `ban_user`
# directly (as `chat.py`'s own websocket auto-ban handler already does).
#
# Bounds reasoning, for a peer-moderated student chat platform:
# - MIN (60s / 1 minute): below this a ban is not a real intervention --
#   Redis TTL granularity and ordinary network/reconnect latency mean a
#   sub-minute ban could expire before it even interrupts anything, and
#   there is no real moderation scenario on this platform ("cool off for
#   a few seconds") that a minimum this low would serve.
# - MAX (86400s / 24 hours): long enough to be a genuine deterrent for a
#   first serious offense, while deliberately NOT letting one moderator
#   unilaterally impose an effectively-permanent ban with a single click
#   -- repeat/escalating misconduct is meant to surface through the
#   existing Repeat-Offender Detection roster (`GET /admin/flagged-users`)
#   for a considered, human decision, not accumulate via one moderator
#   silently re-applying an ever-longer single ban. Also matches this
#   service's own established "self-expiring, no cleanup job needed"
#   design (`ban_user`'s own docstring) -- even an unreviewed 24h ban
#   self-corrects.
BAN_DURATION_MIN_SECONDS = 60
BAN_DURATION_MAX_SECONDS = 86400

_redis_client: aioredis.Redis | None = None


def _get_redis_client() -> aioredis.Redis:
    """Lazy singleton, not a module-level `Redis.from_url(...)` call at
    import time -- constructing the client doesn't itself open a network
    connection (redis-py connects lazily on first command), but building
    it lazily still means importing this module never fails/blocks even
    if `settings.redis_url` is misconfigured until a ban check actually
    runs, and makes the client trivially monkeypatchable in tests (see
    `tests/test_chat.py`'s `_FakeRedis` fixture, which patches this
    function directly rather than needing a real Redis server)."""
    global _redis_client
    if _redis_client is None:
        _redis_client = aioredis.Redis.from_url(settings.redis_url, decode_responses=True)
    return _redis_client


def contains_arabic(text: str) -> bool:
    """This step's own literal filter: `re.search(r"[\\u0600-\\u06FF]",
    text)`, truthy/falsy. Matches ANY single Arabic-block character
    anywhere in `text` -- deliberately not anchored, not requiring a
    whole word, matching the ask's own literal regex exactly (no
    narrowing to "mostly Arabic" or similar heuristics that aren't in
    the ask)."""
    return _ARABIC_CHAR_PATTERN.search(text) is not None


async def ban_user(user_id: uuid.UUID | str, duration_seconds: int = 1800) -> None:
    """This step's own literal function. Sets a Redis key with a TTL of
    `duration_seconds` (default 1800 = 30 minutes, per the ask) -- the
    ban is self-expiring; nothing ever has to run a cleanup job to lift
    it. The stored value itself is never read back for anything other
    than existence/TTL (see `is_user_banned`/`get_ban_remaining_seconds`
    below), so it's just `"1"` -- a placeholder value, not meaningful
    data.

    Manual Timed Bans addition: real bounds enforcement, see
    `BAN_DURATION_MIN_SECONDS`/`BAN_DURATION_MAX_SECONDS`'s own comment
    above for the reasoning. The default (1800s) is deliberately left
    unvalidated-by-construction against a *variable* -- it's a literal
    within bounds, not user input -- but every caller that passes its
    own `duration_seconds` (both real HTTP paths, and the Arabic-filter
    auto-ban's own fixed constant) is checked here regardless of what
    validation, if any, ran upstream.
    """
    if not (BAN_DURATION_MIN_SECONDS <= duration_seconds <= BAN_DURATION_MAX_SECONDS):
        raise ValueError(
            f"duration_seconds must be between {BAN_DURATION_MIN_SECONDS} and "
            f"{BAN_DURATION_MAX_SECONDS} (got {duration_seconds})"
        )
    client = _get_redis_client()
    await client.set(f"{_BAN_KEY_PREFIX}{user_id}", "1", ex=duration_seconds)


async def is_user_banned(user_id: uuid.UUID | str) -> bool:
    """This step's own literal function. `EXISTS` against the ban key --
    if the TTL set by `ban_user` has expired, Redis has already deleted
    the key itself, so this returns `False` with no extra bookkeeping."""
    client = _get_redis_client()
    exists = await client.exists(f"{_BAN_KEY_PREFIX}{user_id}")
    return bool(exists)


async def get_ban_remaining_seconds(user_id: uuid.UUID | str) -> int | None:
    """Disclosed necessary addition -- see module docstring. Returns the
    real remaining TTL in seconds if `user_id` is currently banned, or
    `None` if they are not (covers both "never banned" and "ban already
    expired" -- Redis's own `TTL` command returns `-2` for a key that
    doesn't exist, which this normalizes to `None` rather than leaking
    a Redis-specific sentinel value into the rest of the app)."""
    client = _get_redis_client()
    ttl = await client.ttl(f"{_BAN_KEY_PREFIX}{user_id}")
    if ttl is None or ttl < 0:
        return None
    return int(ttl)


# ---------------------------------------------------------------------
# Student Dashboard & Admin Control Panel additions -- neither of these
# two functions existed before this step. `is_user_banned`/`ban_user`/
# `get_ban_remaining_seconds` above all operate on ONE already-known
# `user_id` at a time; this step's own Admin Hub ask ("List of currently
# banned users from Redis") and Chat Management UI ("Unban Early")
# instead need to (a) discover EVERY currently-banned user with no prior
# knowledge of who they are, and (b) lift a specific ban on demand --
# neither of which the existing two functions can do.
# ---------------------------------------------------------------------


async def list_banned_users() -> list[tuple[uuid.UUID, int]]:
    """Every currently-active chat ban, as `(user_id, remaining_seconds)`
    pairs. Uses Redis's own `SCAN` (via `scan_iter`, redis-py's
    cursor-driven wrapper around it) rather than `KEYS` -- `KEYS` blocks
    the entire Redis server for the duration of the scan on a large
    keyspace, a real production hazard this codebase's own Redis instance
    also serves Celery's broker/result-backend traffic through; `SCAN`
    walks the keyspace incrementally instead, at the cost of not being a
    single atomic snapshot (a ban that expires or is newly created mid-scan
    may or may not be reflected) -- an acceptable trade-off for an admin
    dashboard's "who's currently banned" widget, which is inherently a
    point-in-time-ish read anyway (see `app/api/v1/endpoints/dashboards.py`'s
    own docstring on this same posture for its other aggregate reads).

    Skips (rather than raising on) any key whose suffix isn't a valid
    UUID or whose TTL has already lapsed between the `SCAN` and the
    follow-up `TTL` call (a real, if narrow, race -- the ban expiring in
    that exact window) -- both are treated as "not currently banned,"
    the correct outcome either way, rather than corrupting the whole
    listing over one stale/malformed entry.
    """
    client = _get_redis_client()
    banned: list[tuple[uuid.UUID, int]] = []

    async for key in client.scan_iter(match=f"{_BAN_KEY_PREFIX}*"):
        raw_user_id = key[len(_BAN_KEY_PREFIX):]
        try:
            user_id = uuid.UUID(raw_user_id)
        except ValueError:
            continue

        ttl = await client.ttl(key)
        if ttl is None or ttl < 0:
            continue

        banned.append((user_id, int(ttl)))

    return banned


async def unban_user(user_id: uuid.UUID | str) -> bool:
    """This step's own literal "Unban Early" action -- deletes the Redis
    ban key outright rather than waiting for its TTL to lapse naturally.
    Returns whether a key actually existed to delete (`DEL` returns the
    number of keys removed, `0` or `1` here since this only ever targets
    one specific key) -- lets the router distinguish "successfully lifted
    an active ban" from "this user wasn't banned in the first place" for
    an honest response, rather than reporting success either way."""
    client = _get_redis_client()
    deleted = await client.delete(f"{_BAN_KEY_PREFIX}{user_id}")
    return bool(deleted)
