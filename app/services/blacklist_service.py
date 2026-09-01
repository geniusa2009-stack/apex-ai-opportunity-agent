"""General Content Blacklist / Pattern Auto-Trigger -- service layer.

Owns the one real matching function (`check_content`) every real
content-creation pathway calls, plus the CRUD helpers backing
`app/api/v1/endpoints/admin_blacklist.py`. Kept out of the router, same
3-layer convention as `moderation_log_service.py`/
`moderation_claim_service.py`.

STEP 2 DESIGN, DISCLOSED HERE (trigger response): unlike
`chat_security.contains_arabic` (whose match triggers a real 30-minute
ban -- a response calibrated to ONE specific, hard platform rule),
a blacklist match here REJECTS the content outright (a 422, matching
`guild_service._moderate_content`'s own existing precedent for guild
posts/comments) and does NOT ban anyone. Reasoning: this filter runs
identically across five very different content-creation pathways
(chat, video title/description, guild posts, guild comments, profile
bio) -- a single word-list match is real evidence the specific piece of
content shouldn't be published, but it is NOT the same category of
signal as "this student is deliberately violating the English-only
chat rule," and auto-banning on every hit across every pathway would be
a wildly disproportionate response to, say, one flagged word in a video
description. Repeat/escalating misconduct is meant to surface through
this feature's own new trigger-event log (visible to any Moderator, see
the endpoint module's own docstring) for a considered human decision,
the same "don't let an automated system silently escalate to punishment
on its own" posture `chat_security.py`'s own ban-duration bounds already
established for a different reason.
"""

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.blacklist import BlacklistedPattern
from app.models.enums import ModerationAction, ModerationTargetType
from app.models.moderation_log import ModerationLogEntry
from app.services import moderation_log_service


@dataclass(frozen=True)
class BlacklistMatch:
    """What actually matched -- kept separate from the user-facing
    rejection message (see `app/api/v1/endpoints/admin_blacklist.py`'s
    own docstring for why the two are deliberately different audiences:
    this carries the real pattern, logged for staff; the user only ever
    sees a generic, non-revealing message)."""

    pattern_id: uuid.UUID
    pattern_text: str


def _compile_pattern(pattern: BlacklistedPattern) -> re.Pattern[str]:
    """Case-insensitive always (see `BlacklistedPattern`'s own docstring
    for why case-sensitivity was deliberately not made a second knob).
    `is_regex=False` (the default) wraps the literal text in `\\b...\\b`
    word-boundary anchors via `re.escape` -- the deliberate, honest
    default that avoids the classic "class" catches "ass" false-positive
    a raw substring match would have. `is_regex=True` uses the stored
    text as a real regex source verbatim -- the Owner's own explicit
    opt-in, not validated for "safety" beyond simply failing to compile
    (a malformed regex is caught and skipped, logged, rather than
    crashing every real content-creation pathway on one bad row -- see
    `check_content`'s own try/except below)."""
    if pattern.is_regex:
        return re.compile(pattern.pattern, re.IGNORECASE)
    return re.compile(r"\b" + re.escape(pattern.pattern) + r"\b", re.IGNORECASE)


async def list_patterns(db: AsyncSession) -> list[BlacklistedPattern]:
    result = await db.execute(select(BlacklistedPattern).order_by(BlacklistedPattern.created_at.desc()))
    return list(result.scalars().all())


async def create_pattern(
    db: AsyncSession, *, pattern: str, is_regex: bool, description: str | None, created_by: uuid.UUID
) -> BlacklistedPattern:
    row = BlacklistedPattern(
        pattern=pattern, is_regex=is_regex, description=description, created_by=created_by
    )
    db.add(row)
    await db.commit()
    await db.refresh(row)
    return row


class PatternNotFoundError(Exception):
    pass


async def delete_pattern(db: AsyncSession, pattern_id: uuid.UUID) -> None:
    row = await db.get(BlacklistedPattern, pattern_id)
    if row is None:
        raise PatternNotFoundError(str(pattern_id))
    await db.delete(row)
    await db.commit()


async def check_content(db: AsyncSession, content: str | None) -> BlacklistMatch | None:
    """The one real, shared filter-check function every content-creation
    pathway calls -- see this module's own docstring for the full Step
    2 "why reject, not ban" reasoning. Returns the first real match (or
    `None`) -- callers decide what to do with it (every real call site
    in this codebase raises/rejects on a match, but this function itself
    stays a pure predicate, not an HTTP-layer decision, matching
    `chat_security.contains_arabic`'s own posture of being a plain,
    independently-testable check with no I/O beyond the DB read).

    `content is None`/empty is never a match -- every real caller here
    passes an OPTIONAL field (a video description, a profile bio) that
    can legitimately be absent; there's nothing to check.
    """
    if not content:
        return None

    patterns = await list_patterns(db)
    for pattern in patterns:
        try:
            compiled = _compile_pattern(pattern)
        except re.error:
            # A malformed `is_regex=True` row must never take down every
            # real content-creation pathway on this platform -- skip it,
            # not crash. (An Owner who typos a regex will simply see
            # their pattern silently not match anything, discoverable
            # the same way any other never-triggering pattern is.)
            continue
        if compiled.search(content):
            return BlacklistMatch(pattern_id=pattern.id, pattern_text=pattern.pattern)
    return None


def log_trigger(
    db: AsyncSession,
    *,
    submitting_user_id: uuid.UUID,
    match: BlacklistMatch,
    content_type: str,
    content_snippet: str,
) -> None:
    """Real logging of every trigger event through the SAME
    `ModerationLogEntry` pathway every other automated, system-initiated
    action already uses (`actor_id=None`/`actor_role=None`, same as
    `CHAT_AUTO_BAN` -- the system itself is acting, not a human
    moderator). `target_type=USER`/`target_id=<the submitting user's own
    id>` -- same direct-target convention `CHAT_AUTO_BAN`/`CHAT_UNBAN`/
    `CHAT_MANUAL_BAN` already established; there is no other real "case"
    row to target here (the content itself is never persisted -- it was
    rejected before it could be).

    `content_snippet` -- a real, TRUNCATED excerpt of the rejected text
    (not the full submission) stored in `outcome`, so a Moderator
    reviewing trigger events has real context for what was actually
    caught, without this log entry becoming an unbounded second copy of
    everything anyone has ever tried to submit.
    """
    moderation_log_service.log_action(
        db,
        actor_id=None,
        actor_role=None,
        action=ModerationAction.BLACKLIST_TRIGGERED,
        target_type=ModerationTargetType.USER,
        target_id=submitting_user_id,
        outcome={
            "content_type": content_type,
            "matched_pattern_id": str(match.pattern_id),
            "matched_pattern": match.pattern_text,
            "content_snippet": content_snippet[:200],
        },
    )


async def list_trigger_events(
    db: AsyncSession, *, limit: int, offset: int
) -> tuple[list[ModerationLogEntry], int]:
    """Backs `GET /admin/blacklist/triggers` -- Moderator+ (see this
    module's own module docstring / the endpoint module's docstring for
    the Owner-edits/Moderator-views tier split). Deliberately a
    STANDALONE query against `ModerationLogEntry` filtered to
    `action=BLACKLIST_TRIGGERED`, not a call into
    `moderation_log_service.list_team_log` -- that function backs the
    Owner-only, unfiltered-by-action FULL activity log (still
    `RequireOwner`, per Phase 3's own "only I see it" decision); reusing
    it here would either require loosening that endpoint's own tier or
    duplicating a tier check inline. A dedicated, action-filtered query
    is the honest boundary: a Moderator can see blacklist trigger
    events specifically, and nothing else the full log contains.
    """
    filters = (ModerationLogEntry.action == ModerationAction.BLACKLIST_TRIGGERED.value,)
    total = (
        await db.execute(select(func.count()).select_from(ModerationLogEntry).where(*filters))
    ).scalar_one()
    result = await db.execute(
        select(ModerationLogEntry)
        .where(*filters)
        .order_by(ModerationLogEntry.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return list(result.scalars().all()), total
